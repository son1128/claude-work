"""저장소, 거래소 어댑터, 수집기."""
import pytest
from conftest import FakeHttp, synth

from ppp2.data.collector import Collector
from ppp2.data.exchanges import INTERVAL_MS, BingXFutures, GateFutures, Ticker

M = INTERVAL_MS["1m"]


# ── 저장소 ──────────────────────────────────────────────────────────────────
def test_upsert_is_idempotent_and_updates(store):
    rows = synth(10, "1m")
    assert store.upsert_candles("gate", "BTC", "1m", rows) == 10
    assert store.upsert_candles("gate", "BTC", "1m", rows) == 0
    changed = [(rows[-1][0], 1, 2, 0.5, 1.5, 9)]
    store.upsert_candles("gate", "BTC", "1m", changed)
    assert store.load("gate", "BTC", "1m")["close"].iloc[-1] == 1.5
    assert store.count("gate", "BTC", "1m") == 10


def test_gaps_and_quality(store):
    rows = synth(20, "1m")
    kept = rows[:5] + rows[8:]
    store.upsert_candles("gate", "BTC", "1m", kept)
    assert store.gaps("gate", "BTC", "1m") == [(rows[5][0], rows[7][0], 3)]
    q = store.quality("gate", "BTC", "1m")
    assert q["missing"] == 3 and q["bad_ohlc"] == 0
    store.upsert_candles("gate", "BTC", "1m", [(rows[0][0], 10, 9, 8, 10, 1)])     # 고가 < 시가
    assert store.quality("gate", "BTC", "1m")["bad_ohlc"] == 1


def test_load_limit_returns_latest_in_order(store):
    rows = synth(50, "1m")
    store.upsert_candles("gate", "BTC", "1m", rows)
    d = store.load("gate", "BTC", "1m", limit=5)
    assert list(d["time"]) == [r[0] for r in rows[-5:]]


# ── 거래소 어댑터 ────────────────────────────────────────────────────────────
def gate_rows(start_s, end_s, step_s, price=100.0):
    return [{"t": t, "o": str(price), "h": str(price + 1), "l": str(price - 1), "c": str(price), "v": 5}
            for t in range(start_s, end_s + 1, step_s)]


def test_gate_drops_unfinished_bar_and_clamps_history():
    now = 1_800_000_000_000 // M * M + 30_000              # 분 중간
    http = FakeHttp({("GET", "/futures/usdt/candlesticks"): lambda p, b: gate_rows(p["from"], p["to"], 60)})
    g = GateFutures(http=http, now=lambda: now)
    rows = g.candles("BTC", "1m", now - 20_000 * M)       # 1만 봉보다 오래된 요청
    assert rows[-1][0] + M <= now                          # 마감된 봉만
    assert rows[0][0] >= g.earliest_ms("1m")
    assert all(c[2]["from"] * 1000 >= g.earliest_ms("1m") for c in http.calls)
    assert len(http.calls) >= 5                            # 1999봉씩 나눠 요청


def test_gate_contract_spec_parsing():
    http = FakeHttp({("GET", "/futures/usdt/contracts"): lambda p, b: [
        {"name": "BTC_USDT", "quanto_multiplier": "0.0001", "order_size_min": 1, "order_price_round": "0.1",
         "maintenance_rate": "0.003", "leverage_max": "200", "maker_fee_rate": "-0.0001", "taker_fee_rate": "0.00075"},
        {"name": "BTC_USD", "quanto_multiplier": "1"}]})
    s = GateFutures(http=http).contracts()
    assert len(s) == 1 and s[0].symbol == "BTC" and s[0].mmr == 0.003 and s[0].contract_size == 0.0001


def test_bingx_retries_rate_limit_then_succeeds():
    seq = iter([{"code": 100410, "msg": "请稍后重试"}, {"code": 0, "data": [
        {"time": 1_800_000_000_000, "open": "1", "high": "2", "low": "0.5", "close": "1.5", "volume": "3"}]}])
    http = FakeHttp({("GET", "/openApi/swap/v3/quote/klines"): lambda p, b: next(seq)})
    b = BingXFutures(http=http, now=lambda: 1_800_000_000_000 + 10 * M, sleep=lambda s: None)
    rows = b.candles("BTC", "1m", 1_800_000_000_000, 1_800_000_000_000 + 59_999)
    assert rows == [(1_800_000_000_000, 1.0, 2.0, 0.5, 1.5, 3.0)]


def test_bingx_unknown_symbol_raises():
    http = FakeHttp({("GET", "/openApi/swap/v3/quote/klines"): lambda p, b: {"code": 109425, "msg": "X-USDT not exist"}})
    with pytest.raises(KeyError):
        BingXFutures(http=http, sleep=lambda s: None).candles("X", "1m", 0, 60_000)


# ── 수집기 ──────────────────────────────────────────────────────────────────
class FakeExchange:
    name = "fake"

    def __init__(self, now, fail=()):
        self.now = now
        self.fail = set(fail)
        self.requests = []

    def candles(self, symbol, interval, start, end):
        if symbol in self.fail:
            raise RuntimeError("boom")
        self.requests.append((symbol, interval, start, end))
        step = INTERVAL_MS[interval]
        t = start // step * step
        out = []
        while t <= end and t + step <= self.now:          # end 포함, 마감된 봉만 (실제 거래소와 같음)
            out.append((t, 1.0, 2.0, 0.5, 1.5, 1.0))
            t += step
        return out

    def contracts(self):
        return []

    def tickers(self):
        return {"A": Ticker("A", 1, 1, 5e7, 0), "B": Ticker("B", 1, 1, 9e7, 0), "C": Ticker("C", 1, 1, 1e6, 0)}

    def funding(self, symbol, limit=1000):
        return [(1, 0.0001)]


def test_update_first_uses_lookback_then_incremental(store):
    now = 1_800_000_000_000
    ex = FakeExchange(now)
    col = Collector(store, ex, now=lambda: now)
    assert col.update("BTC", "1m", lookback_days=1) == 1440
    ex.now = now + 5 * M
    col.now = lambda: now + 5 * M
    assert col.update("BTC", "1m", lookback_days=1) == 5
    assert ex.requests[-1][2] == store.last_ts("fake", "BTC", "1m") - 7 * M   # 마지막 봉 2개 앞부터 다시


def test_repair_fills_gap(store):
    now = 1_800_000_000_000
    col = Collector(store, FakeExchange(now), now=lambda: now)
    rows = [(now - (100 - i) * M, 1, 2, 0.5, 1.5, 1) for i in range(100)]
    store.upsert_candles("fake", "BTC", "1m", rows[:40] + rows[60:])
    assert col.repair("BTC", "1m") == 20
    assert store.gaps("fake", "BTC", "1m") == []


def test_one_symbol_failure_does_not_stop_cycle(store):
    now = 1_800_000_000_000
    col = Collector(store, FakeExchange(now, fail={"BAD"}), now=lambda: now)
    res = col.cycle(["BAD", "BTC"], ["1m"], {"1m": 0.01})
    assert isinstance(res[("BAD", "1m")], str) and res[("BTC", "1m")] > 0


def test_universe_ranks_by_volume(store):
    class Ex(FakeExchange):
        def contracts(self):
            from conftest import spec
            return [spec("A"), spec("B"), spec("C")]
    col = Collector(store, Ex(0))
    assert col.universe(top_n=2, min_quote_volume=2e6) == ["B", "A"]


def test_binance_spot_paginates_and_drops_open_bar():
    from ppp2.data.exchanges import BinanceSpotData
    step = INTERVAL_MS["4h"]
    now = 1_800_000_000_000 // step * step + 1000          # 진행 중인 봉이 하나 있음
    start = now - 2500 * step

    def klines(p, b):
        t0 = p["startTime"] // step * step
        ts = [t for t in range(t0, min(p["endTime"], now) + 1, step)][: p["limit"]]
        return [[t, "1", "2", "0.5", "1.5", "3"] for t in ts]
    http = FakeHttp({("GET", "/api/v3/klines"): klines})
    rows = BinanceSpotData(http=http, now=lambda: now).candles("BTC", "4h", start)
    assert len(http.calls) == 3 and rows[-1][0] + step <= now
    assert len(rows) == len({r[0] for r in rows}) == 2500 - 0
