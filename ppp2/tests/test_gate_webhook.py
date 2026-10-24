"""Gate 주문 연결(서명·요청·안전장치)과 웹훅."""
import hashlib
import hmac
import json
import threading
import urllib.error
import urllib.request

import pytest
from conftest import FakeHttp, spec

from ppp2.config import ExitCfg, RiskCfg
from ppp2.engine.gate import GateBroker, GateDryRun, GateRequests, sign
from ppp2.engine.models import Signal
from ppp2.engine.risk import AccountContext, RiskEngine
from ppp2.engine.webhook import make_handler, normalize_symbol, parse_alert

NOW = 1_800_000_000_000


def test_sign_matches_gate_v4_format():
    body = '{"contract":"BTC_USDT","size":1}'
    expected = hmac.new(b"s3cret", "\n".join(["POST", "/api/v4/futures/usdt/orders", "a=1",
                                               hashlib.sha512(body.encode()).hexdigest(), "1700000000"]).encode(),
                        hashlib.sha512).hexdigest()
    assert sign("s3cret", "POST", "/api/v4/futures/usdt/orders", "a=1", body, "1700000000") == expected
    assert sign("s3cret", "GET", "/p", "", "", "1") == hmac.new(
        b"s3cret", f"GET\n/p\n\n{hashlib.sha512(b'').hexdigest()}\n1".encode(), hashlib.sha512).hexdigest()


def test_request_builders():
    r = GateRequests()
    m, p, q, b = r.stop("BTC", 1, 79_000.5, 0)
    assert p == "/futures/usdt/price_orders" and b["trigger"]["rule"] == 2 and b["initial"]["close"] is True
    assert r.stop("BTC", -1, 81_000, 1)[3]["trigger"]["rule"] == 1
    assert r.close("BTC", 1, 7)[3]["size"] == -7 and r.close("BTC", -1, 7)[3]["size"] == 7
    assert r.tp1("BTC", 1, 3, 82_000)[3]["reduce_only"] is True
    assert r.leverage("BTC", 5)[2] == {"leverage": "5"}


def plan(side=1, entry=80_000.0, stop=79_200.0):
    d = RiskEngine(RiskCfg(), ExitCfg()).check(Signal("BTC", side, entry, NOW, stop=stop, l1=side),
                                               AccountContext(10_000, 10_000, NOW, entry, spec()))
    assert d.approved, d.summary()
    return d.plan


def gate_routes(pl, liq, fail_stop=False, pos_size=None):
    state = {"size": int(pl.side * pl.qty) if pos_size is None else pos_size}

    def orders(p, body):
        b = json.loads(body)
        if b.get("reduce_only"):
            return {"id": 77}
        return {"id": 1, "size": b["size"], "left": 0, "fill_price": "80000"}

    def price_orders(p, body):
        if fail_stop:
            raise RuntimeError("거래소 오류")
        return {"id": 9}
    return state, {
        ("GET", "/futures/usdt/accounts"): lambda p, b: {"total": "10000", "available": "9000", "unrealised_pnl": "0", "in_dual_mode": False},
        ("POST", "/futures/usdt/positions/BTC_USDT/leverage"): lambda p, b: {},
        ("POST", "/futures/usdt/orders"): orders,
        ("GET", "/futures/usdt/positions/BTC_USDT"): lambda p, b: {"size": state["size"], "entry_price": "80000",
                                                                  "liq_price": str(liq), "margin": "500"},
        ("POST", "/futures/usdt/price_orders"): price_orders,
        ("DELETE", "/futures/usdt/price_orders/*"): lambda p, b: {},
        ("DELETE", "/futures/usdt/orders/*"): lambda p, b: {},
    }


def broker(routes):
    http = FakeHttp(routes)
    return GateBroker("k", "s", testnet=True, http=http), http


def test_open_places_entry_then_stop_then_tp1_with_signature():
    pl = plan()
    _, routes = gate_routes(pl, liq=60_000)
    b, http = broker(routes)
    pos = b.open(pl, NOW, "15m", NOW, 0.0001)
    seq = [(m, p) for m, p, *_ in http.calls]
    assert seq.index(("POST", "/futures/usdt/orders")) < seq.index(("POST", "/futures/usdt/price_orders"))
    assert pos.exchange_ref["stop_order"] == 9 and pos.exchange_ref["tp1_order"] == 77
    assert all({"KEY", "SIGN", "Timestamp"} <= set(c[4]) for c in http.calls)
    lev_call = [c for c in http.calls if c[1].endswith("/leverage")][0]
    assert lev_call[2] == {"leverage": str(pl.leverage)}


def test_exchange_liq_inside_stop_triggers_emergency_close():
    pl = plan()
    _, routes = gate_routes(pl, liq=79_500)          # 거래소 청산가가 손절가(79,200)보다 위
    b, http = broker(routes)
    with pytest.raises(RuntimeError):
        b.open(pl, NOW, "15m", NOW, 0.0001)
    closes = [json.loads(c[3]) for c in http.calls if c[1] == "/futures/usdt/orders" and c[3] and json.loads(c[3]).get("reduce_only")]
    assert closes and closes[0]["size"] == -pl.qty
    assert not b.open_positions() and b.closed[0].exit_reason == "긴급 정리"


def test_stop_order_failure_closes_position():
    pl = plan()
    _, routes = gate_routes(pl, liq=60_000, fail_stop=True)
    b, http = broker(routes)
    with pytest.raises(RuntimeError):
        b.open(pl, NOW, "15m", NOW, 0.0001)
    assert b.closed and b.closed[0].exit_reason == "긴급 정리"


def test_sync_detects_tp1_and_exchange_stop():
    pl = plan()
    state, routes = gate_routes(pl, liq=60_000)
    b, http = broker(routes)
    pos = b.open(pl, NOW, "15m", NOW, 0.0001)
    state["size"] = int(pl.qty - pl.tp1_qty)
    b.sync(NOW + 1)
    assert pos.tp1_done and pos.qty == pl.qty - pl.tp1_qty
    state["size"] = 0
    b.sync(NOW + 2)
    assert pos.status == "closed" and pos.exit_reason == "본전"


def test_modify_stop_places_new_before_cancelling_old():
    pl = plan()
    _, routes = gate_routes(pl, liq=60_000)
    b, http = broker(routes)
    pos = b.open(pl, NOW, "15m", NOW, 0.0001)
    n = len(http.calls)
    b.modify_stop(pos, pos.entry_price, NOW + 1)
    seq = [(m, p.split("/")[-1] if m == "DELETE" else p) for m, p, *_ in http.calls[n:]]
    assert seq[0] == ("POST", "/futures/usdt/price_orders") and seq[1][0] == "DELETE"
    assert pos.stop_price == pos.entry_price


def test_dual_mode_refused():
    pl = plan()
    _, routes = gate_routes(pl, liq=60_000)
    routes[("GET", "/futures/usdt/accounts")] = lambda p, b: {"total": "1", "available": "1", "in_dual_mode": True}
    b, _ = broker(routes)
    with pytest.raises(RuntimeError, match="dual"):
        b.open(pl, NOW, "15m", NOW, 0.0001)


def test_dryrun_logs_requests_without_network():
    pl = plan()
    b = GateDryRun(equity=10_000)
    b.open(pl, NOW, "15m", NOW, 0.0001)
    reqs = [e for e in b.drain_events() if e["type"] == "gate_request"]
    assert [r["path"] for r in reqs][:3] == ["/futures/usdt/positions/BTC_USDT/leverage", "/futures/usdt/orders",
                                             "/futures/usdt/price_orders"]


# ── 웹훅 ────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("raw,sym", [("BTCUSDT.P", "BTC"), ("BINANCE:ETHUSDT", "ETH"), ("SOL_USDT", "SOL"),
                                     ("WLD-USDT", "WLD"), ("1000PEPEUSDT.P", "1000PEPE"), ("BTCUSDTPERP", "BTC")])
def test_normalize_symbol(raw, sym):
    assert normalize_symbol(raw) == sym


def test_parse_pine_v2_alert():
    s = parse_alert({"symbol": "BTCUSDT.P", "tf": "15", "side": "long", "score": 70, "price": 80123.4,
                     "stop": 79500, "l1": 1}, NOW)
    assert (s.symbol, s.side, s.tf, s.stop, s.l1, s.score) == ("BTC", 1, "15m", 79500.0, 1, 70.0)
    with pytest.raises(ValueError):
        parse_alert({"symbol": "BTC", "side": "up", "price": 1}, NOW)


class StubEngine:
    def __init__(self):
        self.got = []

        class B:
            def open_positions(self):
                return []
        self.broker = B()

    def halted(self):
        return False, ""

    def submit(self, sig):
        self.got.append(sig)

        class D:
            approved = True
            failed = []

            def summary(self):
                return "ok"
        return D()


def test_webhook_server_token_and_submit():
    from http.server import ThreadingHTTPServer
    eng = StubEngine()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(eng, "tok"))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    body = json.dumps({"symbol": "BTCUSDT.P", "side": "short", "price": 80000, "stop": 80800, "l1": -1}).encode()
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(urllib.request.Request(url + "/signal?token=bad", data=body, method="POST"))
    assert e.value.code == 401
    with urllib.request.urlopen(urllib.request.Request(url + "/signal?token=tok", data=body, method="POST")) as r:
        assert json.load(r)["approved"] is True
    assert eng.got[0].symbol == "BTC" and eng.got[0].side == -1
    with urllib.request.urlopen(url + "/health") as r:
        assert json.load(r)["ok"] is True
    srv.shutdown()
