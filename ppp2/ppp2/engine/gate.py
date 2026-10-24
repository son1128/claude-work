"""Gate 무기한 선물(USDT) 주문 연결.

  GateRequests   주문 요청 본문 만들기 (진입·손절·1차 익절·청산·레버리지). 실거래와 드라이런이 같은 함수를 씁니다.
  sign()         Gate APIv4 서명 (HMAC-SHA512)
  GateDryRun     모의 체결(PaperBroker) + "실거래라면 보냈을 요청"을 이벤트로 기록. 키 없이 동작
  GateBroker     실제 주문. 기본은 테스트넷. 진입 직후 거래소가 계산한 청산가를 다시 읽어
                 손절이 청산보다 먼저 오는지 확인하고, 아니거나 손절 주문이 실패하면 즉시 시장가로 정리합니다.

전제: 단방향 포지션 모드(dual mode 꺼짐), 레버리지를 숫자로 지정 = 격리 마진.
주의: 실계정·테스트넷 키로 검증하지 않은 코드입니다. 테스트넷에서 먼저 확인하세요.
"""
import hashlib
import hmac
import json
import os
import time
import urllib.parse
from dataclasses import asdict

from ..data.http import Http
from .models import Position
from .paper import PaperBroker

LIVE_BASE = "https://api.gateio.ws/api/v4"
TESTNET_BASE = "https://fx-api-testnet.gateio.ws/api/v4"


def sign(secret, method, path, query, body, ts):
    """Gate APIv4: HexEncode(HMAC_SHA512(secret, METHOD\\nPATH\\nQUERY\\nHexEncode(SHA512(BODY))\\nTIMESTAMP))."""
    hashed = hashlib.sha512((body or "").encode()).hexdigest()
    msg = f"{method}\n{path}\n{query}\n{hashed}\n{ts}"
    return hmac.new(secret.encode(), msg.encode(), hashlib.sha512).hexdigest()


def _px(x):
    return f"{x:.10f}".rstrip("0").rstrip(".")


class GateRequests:
    """(method, path, query, body) 튜플을 만듭니다. size는 계약 수(정수), 숏은 음수."""

    @staticmethod
    def contract(symbol):
        return f"{symbol.upper()}_USDT"

    def leverage(self, symbol, lev):
        return ("POST", f"/futures/usdt/positions/{self.contract(symbol)}/leverage", {"leverage": str(int(lev))}, None)

    def entry(self, plan, tag):
        return ("POST", "/futures/usdt/orders", None,
                {"contract": self.contract(plan.symbol), "size": int(plan.side * plan.qty), "price": "0", "tif": "ioc",
                 "text": f"t-{tag}"[:28]})

    def stop(self, symbol, side, stop_price, price_type=0):
        # 남은 물량 전체를 닫는 조건부 주문. 롱은 가격 ≤ 손절가(rule 2), 숏은 ≥ 손절가(rule 1)
        return ("POST", "/futures/usdt/price_orders", None,
                {"initial": {"contract": self.contract(symbol), "size": 0, "price": "0", "tif": "ioc",
                             "reduce_only": True, "close": True},
                 "trigger": {"strategy_type": 0, "price_type": int(price_type), "price": _px(stop_price),
                             "rule": 2 if side > 0 else 1, "expiration": 0}})

    def tp1(self, symbol, side, qty, price):
        return ("POST", "/futures/usdt/orders", None,
                {"contract": self.contract(symbol), "size": int(-side * qty), "price": _px(price), "tif": "gtc",
                 "reduce_only": True})

    def close(self, symbol, side, qty):
        return ("POST", "/futures/usdt/orders", None,
                {"contract": self.contract(symbol), "size": int(-side * qty), "price": "0", "tif": "ioc", "reduce_only": True})

    def cancel_price_order(self, order_id):
        return ("DELETE", f"/futures/usdt/price_orders/{order_id}", None, None)

    def cancel_order(self, order_id):
        return ("DELETE", f"/futures/usdt/orders/{order_id}", None, None)


class GateDryRun(PaperBroker):
    """키 없이 실거래 흐름을 연습합니다. 체결은 모의, 요청은 기록만."""
    name = "gate-dryrun"

    def __init__(self, price_type=0, **kw):
        super().__init__(**kw)
        self.req = GateRequests()
        self.price_type = price_type

    def _log(self, pos, ts, r):
        self._emit("gate_request", pos, ts, method=r[0], path=r[1], query=r[2], body=r[3])

    def open(self, plan, ts_ms, tf, bar_ts, contract_size):
        pos = super().open(plan, ts_ms, tf, bar_ts, contract_size)
        self._log(pos, ts_ms, self.req.leverage(plan.symbol, plan.leverage))
        self._log(pos, ts_ms, self.req.entry(plan, f"ppp2-{pos.id}"))
        self._log(pos, ts_ms, self.req.stop(plan.symbol, plan.side, plan.stop_price, self.price_type))
        if plan.tp1_qty > 0:
            self._log(pos, ts_ms, self.req.tp1(plan.symbol, plan.side, plan.tp1_qty, plan.tp1_price))
        return pos

    def modify_stop(self, pos, new_stop, ts_ms):
        self._log(pos, ts_ms, self.req.cancel_price_order("<손절 주문 id>"))
        self._log(pos, ts_ms, self.req.stop(pos.symbol, pos.side, new_stop, self.price_type))
        super().modify_stop(pos, new_stop, ts_ms)

    def close(self, pos, price, ts_ms, reason):
        if pos.status == "open":
            self._log(pos, ts_ms, self.req.close(pos.symbol, pos.side, pos.qty))
        super().close(pos, price, ts_ms, reason)


class GateBroker:
    """실제 주문. 기본 testnet=True. dry_run이 필요하면 GateDryRun을 쓰세요."""
    name = "gate"

    def __init__(self, key=None, secret=None, testnet=True, price_type=0, http=None):
        self.key = key or os.environ.get("GATE_API_KEY")
        self.secret = secret or os.environ.get("GATE_API_SECRET")
        if not self.key or not self.secret:
            raise RuntimeError("GATE_API_KEY / GATE_API_SECRET 환경 변수가 필요합니다")
        base = TESTNET_BASE if testnet else LIVE_BASE
        self.http = http or Http(base, min_interval=0.1)
        self.prefix = urllib.parse.urlparse(base).path
        self.req = GateRequests()
        self.price_type = price_type
        self.positions = {}
        self.closed = []
        self.events = []
        self._next = 1
        self._acct = None

    # ── 요청 ────────────────────────────────────────────────────────────────
    def call(self, method, path, query=None, body=None):
        q = urllib.parse.urlencode(query or {})
        b = json.dumps(body, separators=(",", ":")) if body is not None else ""
        ts = str(int(time.time()))
        headers = {"KEY": self.key, "Timestamp": ts, "SIGN": sign(self.secret, method, self.prefix + path, q, b, ts)}
        return self.http.request(method, path, params=query, body=b if body is not None else None, headers=headers)

    def _do(self, r):
        return self.call(r[0], r[1], r[2], r[3])

    def _emit(self, kind, pos, ts, **kw):
        self.events.append({"type": kind, "position_id": pos.id if pos else None, "symbol": pos.symbol if pos else None,
                            "ts_ms": ts, **kw})

    def drain_events(self):
        ev, self.events = self.events, []
        return ev

    # ── 계좌 ────────────────────────────────────────────────────────────────
    def account(self, refresh=False):
        if refresh or self._acct is None:
            self._acct = self.call("GET", "/futures/usdt/accounts")
            if self._acct.get("in_dual_mode"):
                raise RuntimeError("양방향(dual) 포지션 모드입니다. 단방향 모드로 바꾼 뒤 실행하세요.")
        return self._acct

    def equity(self, prices=None):
        a = self.account()
        return float(a.get("total") or 0) + float(a.get("unrealised_pnl") or 0)

    def free(self, prices=None):
        return float(self.account().get("available") or 0)

    def open_positions(self):
        return [p for p in self.positions.values() if p.status == "open"]

    def _position(self, symbol):
        return self.call("GET", f"/futures/usdt/positions/{self.req.contract(symbol)}")

    # ── 주문 ────────────────────────────────────────────────────────────────
    def open(self, plan, ts_ms, tf, bar_ts, contract_size):
        self.account(refresh=True)
        self._do(self.req.leverage(plan.symbol, plan.leverage))
        o = self._do(self.req.entry(plan, f"ppp2-{self._next}"))
        filled = abs(int(o.get("size", 0))) - abs(int(o.get("left", 0)))
        if filled <= 0:
            raise RuntimeError(f"진입 미체결: {o}")
        p = self._position(plan.symbol)
        entry = float(p.get("entry_price") or o.get("fill_price") or plan.entry_price)
        liq = float(p.get("liq_price") or 0)
        pos = Position(id=self._next, symbol=plan.symbol, side=plan.side, entry_price=entry, qty=filled, init_qty=filled,
                       leverage=plan.leverage, margin=float(p.get("margin") or plan.margin), stop_price=plan.stop_price,
                       tp1_price=plan.tp1_price, tp1_qty=min(plan.tp1_qty, filled // 2) if plan.tp1_qty else 0,
                       liq_price=liq, opened_ms=ts_ms, tf=tf, entry_bar_ts=bar_ts, contract_size=contract_size,
                       exchange_ref={"entry_order": o.get("id")})
        self._next += 1
        self.positions[pos.id] = pos
        self._emit("entry", pos, ts_ms, price=entry, qty=filled, leverage=plan.leverage, liq=liq)
        # 거래소가 계산한 청산가로 다시 확인
        if liq and plan.side * (plan.stop_price - liq) <= 0:
            self._emergency_close(pos, ts_ms, f"손절가 {plan.stop_price}가 거래소 청산가 {liq} 밖")
            raise RuntimeError("손절이 청산보다 늦어 즉시 정리했습니다")
        try:
            so = self._do(self.req.stop(plan.symbol, plan.side, plan.stop_price, self.price_type))
            pos.exchange_ref["stop_order"] = so.get("id")
        except Exception as e:
            self._emergency_close(pos, ts_ms, f"손절 주문 실패: {e}")
            raise
        if pos.tp1_qty > 0:
            try:
                to = self._do(self.req.tp1(plan.symbol, plan.side, pos.tp1_qty, plan.tp1_price))
                pos.exchange_ref["tp1_order"] = to.get("id")
            except Exception as e:                      # 1차 익절은 없어도 손절은 살아 있으므로 기록만
                pos.tp1_qty = 0
                self._emit("warning", pos, ts_ms, message=f"1차 익절 주문 실패: {e}")
        return pos

    def _emergency_close(self, pos, ts_ms, why):
        self._emit("emergency_close", pos, ts_ms, reason=why)
        self._do(self.req.close(pos.symbol, pos.side, pos.qty))
        pos.status, pos.exit_reason, pos.closed_ms, pos.qty = "closed", "긴급 정리", ts_ms, 0
        self.closed.append(pos)
        del self.positions[pos.id]

    def modify_stop(self, pos, new_stop, ts_ms):
        old_id = pos.exchange_ref.get("stop_order")
        try:
            so = self._do(self.req.stop(pos.symbol, pos.side, new_stop, self.price_type))   # 새 손절을 먼저 걸고
        except Exception as e:
            self._emit("warning", pos, ts_ms, message=f"손절 이동 실패, 기존 손절 유지: {e}")
            return
        if old_id:
            try:
                self._do(self.req.cancel_price_order(old_id))                               # 그다음 옛 손절 취소
            except Exception as e:
                self._emit("warning", pos, ts_ms, message=f"옛 손절 취소 실패: {e}")
        pos.exchange_ref["stop_order"] = so.get("id")
        self._emit("stop_moved", pos, ts_ms, old=pos.stop_price, new=new_stop)
        pos.stop_price = new_stop

    def close(self, pos, price, ts_ms, reason):
        if pos.status != "open":
            return
        for key, fn in (("tp1_order", self.req.cancel_order), ("stop_order", self.req.cancel_price_order)):
            if pos.exchange_ref.get(key):
                try:
                    self._do(fn(pos.exchange_ref[key]))
                except Exception:
                    pass
        self._do(self.req.close(pos.symbol, pos.side, pos.qty))
        self._emit("exit", pos, ts_ms, price=price, qty=pos.qty, reason=reason)
        pos.status, pos.exit_reason, pos.closed_ms, pos.qty = "closed", reason, ts_ms, 0
        self.closed.append(pos)
        del self.positions[pos.id]

    def sync(self, ts_ms):
        """거래소 포지션 크기로 1차 익절 체결과 거래소 쪽 손절·청산을 알아냅니다."""
        self._acct = None
        for pos in list(self.open_positions()):
            p = self._position(pos.symbol)
            size = abs(int(p.get("size") or 0))
            if size == 0:
                if pos.exchange_ref.get("tp1_order"):
                    try:
                        self._do(self.req.cancel_order(pos.exchange_ref["tp1_order"]))
                    except Exception:
                        pass
                reason = "본전" if pos.tp1_done else "손절"
                self._emit("exit", pos, ts_ms, qty=pos.qty, reason=reason + " (거래소)")
                pos.status, pos.exit_reason, pos.closed_ms, pos.qty = "closed", reason, ts_ms, 0
                self.closed.append(pos)
                del self.positions[pos.id]
            elif size < pos.qty:
                if not pos.tp1_done:
                    pos.tp1_done = True
                    self._emit("exit", pos, ts_ms, qty=pos.qty - size, reason="1차 익절")
                pos.qty = size

    def save_state(self):
        return {"next": self._next, "positions": [asdict(p) for p in self.open_positions()]}

    def load_state(self, st):
        if st:
            self._next = st["next"]
            self.positions = {p["id"]: Position(**p) for p in st["positions"]}
