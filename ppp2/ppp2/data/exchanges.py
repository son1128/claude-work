"""무기한 선물 공개 시세 어댑터 (Gate, BingX).

모든 어댑터는 같은 형태를 돌려줍니다.
  candles(symbol, interval, start_ms, end_ms) -> [(ts_ms, open, high, low, close, volume)]  (마감된 봉만, 시간순)
  contracts()                                 -> [ContractSpec]
  tickers()                                   -> {symbol: Ticker}
  funding(symbol, limit)                      -> [(ts_ms, rate)]
symbol은 기초자산 이름("BTC")이고, 거래소별 표기(BTC_USDT, BTC-USDT)는 어댑터가 바꿉니다.
"""
import time
from dataclasses import dataclass

from .http import Http

INTERVAL_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000, "1h": 3_600_000,
               "4h": 14_400_000, "1d": 86_400_000}


@dataclass
class ContractSpec:
    exchange: str
    symbol: str
    contract_size: float      # 계약 1개가 나타내는 기초자산 수량
    min_qty: float            # 최소 주문 계약 수
    qty_step: float           # 주문 계약 수 단위
    price_tick: float
    mmr: float                # 유지 증거금률 (기본 구간)
    max_leverage: float
    maker_fee: float
    taker_fee: float
    active: bool = True


@dataclass
class Ticker:
    symbol: str
    last: float
    mark: float
    quote_volume_24h: float
    funding_rate: float
    bid: float = 0.0
    ask: float = 0.0


def _now_ms():
    return int(time.time() * 1000)


class GateFutures:
    name = "gate"
    MAX_POINTS = 10_000          # Gate는 시간봉별 최근 1만 봉만 제공
    BATCH = 1999

    def __init__(self, http=None, base="https://api.gateio.ws/api/v4", now=_now_ms):
        self.http = http or Http(base, min_interval=0.12)
        self.now = now

    @staticmethod
    def contract(symbol):
        return f"{symbol.upper()}_USDT"

    def earliest_ms(self, interval):
        step = INTERVAL_MS[interval]
        return (self.now() // step - (self.MAX_POINTS - 10)) * step

    def candles(self, symbol, interval, start_ms, end_ms=None):
        step = INTERVAL_MS[interval]
        now = self.now()
        end_ms = min(end_ms or now, now)
        start_ms = max(start_ms, self.earliest_ms(interval))
        out = {}
        t = start_ms
        while t <= end_ms:
            e = min(t + self.BATCH * step, end_ms)
            rows = self.http.get("/futures/usdt/candlesticks", {
                "contract": self.contract(symbol), "interval": interval, "from": t // 1000, "to": e // 1000})
            for k in rows or []:
                ts = int(k["t"]) * 1000
                if ts + step <= now:                      # 마감된 봉만
                    out[ts] = (ts, float(k["o"]), float(k["h"]), float(k["l"]), float(k["c"]), float(k.get("v") or 0))
            t = e + step
        return [out[k] for k in sorted(out)]

    def contracts(self):
        specs = []
        for c in self.http.get("/futures/usdt/contracts") or []:
            if not c.get("name", "").endswith("_USDT"):
                continue
            specs.append(ContractSpec(
                exchange=self.name, symbol=c["name"][:-5],
                contract_size=float(c.get("quanto_multiplier") or 1), min_qty=float(c.get("order_size_min") or 1),
                qty_step=1.0, price_tick=float(c.get("order_price_round") or 0),
                mmr=float(c.get("maintenance_rate") or 0.01), max_leverage=float(c.get("leverage_max") or 1),
                maker_fee=float(c.get("maker_fee_rate") or 0), taker_fee=float(c.get("taker_fee_rate") or 0),
                active=not c.get("in_delisting", False)))
        return specs

    def tickers(self):
        out = {}
        for t in self.http.get("/futures/usdt/tickers") or []:
            name = t.get("contract", "")
            if not name.endswith("_USDT"):
                continue
            out[name[:-5]] = Ticker(symbol=name[:-5], last=float(t.get("last") or 0), mark=float(t.get("mark_price") or 0),
                                    quote_volume_24h=float(t.get("volume_24h_quote") or 0),
                                    funding_rate=float(t.get("funding_rate") or 0),
                                    bid=float(t.get("highest_bid") or 0), ask=float(t.get("lowest_ask") or 0))
        return out

    def funding(self, symbol, limit=1000):
        rows = self.http.get("/futures/usdt/funding_rate", {"contract": self.contract(symbol), "limit": limit}) or []
        return sorted((int(r["t"]) * 1000, float(r["r"])) for r in rows)


class BingXFutures:
    name = "bingx"
    BATCH = 1000

    def __init__(self, http=None, base="https://open-api.bingx.com", default_mmr=0.01, now=_now_ms, sleep=time.sleep):
        self.http = http or Http(base, min_interval=0.25)
        self.default_mmr = default_mmr
        self.now = now
        self._sleep = sleep

    @staticmethod
    def pair(symbol):
        return f"{symbol.upper()}-USDT"

    def _data(self, path, params=None):
        """BingX는 HTTP 200에 code≠0으로 오류를 돌려줍니다. 호출 제한 응답은 기다렸다 다시 시도합니다."""
        for attempt in range(6):
            d = self.http.get(path, params) or {}
            if d.get("code") == 0:
                return d.get("data")
            msg = str(d.get("msg"))
            if "not exist" in msg or "不存在" in msg:
                raise KeyError(msg)
            self._sleep(min(30, 2 * 2 ** attempt))
        raise RuntimeError(f"BingX {path}: {msg}")

    def candles(self, symbol, interval, start_ms, end_ms=None):
        step = INTERVAL_MS[interval]
        now = self.now()
        end_ms = min(end_ms or now, now)
        out = {}
        t = start_ms
        while t <= end_ms:
            e = min(t + self.BATCH * step - 1, end_ms)
            for k in self._data("/openApi/swap/v3/quote/klines", {
                    "symbol": self.pair(symbol), "interval": interval, "startTime": t, "endTime": e, "limit": 1000}) or []:
                ts = int(k["time"])
                if ts + step <= now:
                    out[ts] = (ts, float(k["open"]), float(k["high"]), float(k["low"]), float(k["close"]), float(k["volume"]))
            t = e + 1
        return [out[k] for k in sorted(out)]

    def contracts(self):
        specs = []
        for c in self._data("/openApi/swap/v2/quote/contracts") or []:
            sym = c.get("symbol", "")
            if not sym.endswith("-USDT"):
                continue
            qp = int(c.get("quantityPrecision") or 0)
            specs.append(ContractSpec(
                exchange=self.name, symbol=sym[:-5], contract_size=1.0,
                min_qty=float(c.get("tradeMinQuantity") or 10 ** -qp), qty_step=10 ** -qp,
                price_tick=10 ** -int(c.get("pricePrecision") or 0), mmr=self.default_mmr,
                max_leverage=float(c.get("maxLongLeverage") or 50),
                maker_fee=float(c.get("makerFeeRate") or 0.0002), taker_fee=float(c.get("takerFeeRate") or 0.0005),
                active=int(c.get("status") or 0) == 1))
        return specs

    def tickers(self):
        out = {}
        for t in self._data("/openApi/swap/v2/quote/ticker") or []:
            sym = t.get("symbol", "")
            if not sym.endswith("-USDT"):
                continue
            last = float(t.get("lastPrice") or 0)
            out[sym[:-5]] = Ticker(symbol=sym[:-5], last=last, mark=last, quote_volume_24h=float(t.get("quoteVolume") or 0),
                                   funding_rate=0.0, bid=float(t.get("bidPrice") or 0), ask=float(t.get("askPrice") or 0))
        return out

    def funding(self, symbol, limit=1000):
        rows = self._data("/openApi/swap/v2/quote/fundingRate", {"symbol": self.pair(symbol), "limit": limit}) or []
        return sorted((int(r["fundingTime"]), float(r["fundingRate"])) for r in rows)


class BinanceSpotData:
    """바이낸스 현물 공개 시세 (data-api.binance.vision). 연구용 장기 이력 전용: 주문·계약 정보 없음.
    선물 API가 막힌 환경에서도 열리고, 상장 폐지된 페어(LUNA, FTT 등)의 과거 봉도 받을 수 있어
    생존 편향을 줄이는 데 씁니다."""
    name = "binance-spot"
    BATCH = 1000

    def __init__(self, http=None, base="https://data-api.binance.vision", now=_now_ms):
        self.http = http or Http(base, min_interval=0.1)
        self.now = now

    def candles(self, symbol, interval, start_ms, end_ms=None):
        step = INTERVAL_MS[interval]
        now = self.now()
        end_ms = min(end_ms or now, now)
        out = {}
        t = start_ms
        while t <= end_ms:
            rows = self.http.get("/api/v3/klines", {"symbol": f"{symbol.upper()}USDT", "interval": interval,
                                                     "startTime": t, "endTime": end_ms, "limit": self.BATCH}) or []
            for k in rows:
                ts = int(k[0])
                if ts + step <= now:
                    out[ts] = (ts, float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5]))
            if len(rows) < self.BATCH:
                break
            t = int(rows[-1][0]) + step
        return [out[k] for k in sorted(out)]

    def contracts(self):
        return []

    def tickers(self):
        return {}

    def funding(self, symbol, limit=1000):
        return []


def make_exchange(name, **kw):
    name = name.lower()
    if name == "gate":
        return GateFutures(**kw)
    if name == "bingx":
        return BingXFutures(**kw)
    if name == "binance-spot":
        return BinanceSpotData(**kw)
    raise ValueError(f"지원하지 않는 거래소: {name} (gate, bingx, binance-spot)")
