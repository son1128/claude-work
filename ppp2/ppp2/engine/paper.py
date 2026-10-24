"""모의 체결 브로커.

실제 시세 봉으로 체결을 흉내 냅니다. 규칙은 백테스트(verify/ppp_v2.py)와 같습니다.
  - 진입: 봉 마감가에 시장가 (슬리피지 적용, 시장가 수수료)
  - 손절: 봉 안에서 손절가에 닿으면 손절가(갭이면 시가)에 체결. 손절과 1차 익절이 같은 봉이면 손절 먼저
  - 1차 익절: 지정가 (지정가 수수료), 봉 안에서 닿으면 체결
  - 시가가 이미 청산가를 넘었으면(갭) 청산: 격리 증거금 전부 손실
"""
from dataclasses import asdict

from .models import Position


class PaperBroker:
    name = "paper"

    def __init__(self, equity=10_000.0, taker_fee=0.00075, maker_fee=-0.0001, slippage_bps=2.0):
        self.cash = float(equity)
        self.taker = taker_fee
        self.maker = maker_fee
        self.slip = slippage_bps / 10_000
        self.positions = {}
        self.closed = []
        self._next = 1
        self.events = []

    # ── 계좌 ────────────────────────────────────────────────────────────────
    def open_positions(self):
        return [p for p in self.positions.values() if p.status == "open"]

    def equity(self, prices):
        u = 0.0
        for p in self.open_positions():
            px = prices.get(p.symbol, p.entry_price)
            u += p.side * (px - p.entry_price) * p.qty * p.contract_size
        return self.cash + u

    def free(self, prices):
        return self.equity(prices) - sum(p.margin * p.qty / p.init_qty for p in self.open_positions())

    def _emit(self, kind, pos, ts, **kw):
        ev = {"type": kind, "position_id": pos.id, "symbol": pos.symbol, "ts_ms": ts, **kw}
        self.events.append(ev)
        return ev

    def drain_events(self):
        ev, self.events = self.events, []
        return ev

    # ── 주문 ────────────────────────────────────────────────────────────────
    def open(self, plan, ts_ms, tf, bar_ts, contract_size):
        fill = plan.entry_price * (1 + plan.side * self.slip)
        notional = plan.qty * contract_size * fill
        fee = notional * self.taker
        self.cash -= fee
        liq = fill * (1 - plan.side * (1 / plan.leverage - plan.mmr))
        pos = Position(id=self._next, symbol=plan.symbol, side=plan.side, entry_price=fill, qty=plan.qty, init_qty=plan.qty,
                       leverage=plan.leverage, margin=notional / plan.leverage, stop_price=plan.stop_price,
                       tp1_price=plan.tp1_price, tp1_qty=plan.tp1_qty, liq_price=liq, opened_ms=ts_ms, tf=tf,
                       entry_bar_ts=bar_ts, contract_size=contract_size, fees=fee)
        self._next += 1
        self.positions[pos.id] = pos
        self._emit("entry", pos, ts_ms, price=fill, qty=plan.qty, fee=fee, leverage=plan.leverage, liq=liq)
        return pos

    def modify_stop(self, pos, new_stop, ts_ms):
        old = pos.stop_price
        pos.stop_price = new_stop
        self._emit("stop_moved", pos, ts_ms, old=old, new=new_stop)

    def _fill(self, pos, qty, price, fee_rate, ts_ms, reason):
        pnl = pos.side * (price - pos.entry_price) * qty * pos.contract_size
        fee = qty * pos.contract_size * price * fee_rate
        self.cash += pnl - fee
        pos.realized += pnl
        pos.fees += fee
        pos.qty = round(pos.qty - qty, 12)
        self._emit("exit", pos, ts_ms, price=price, qty=qty, pnl=pnl, fee=fee, reason=reason)
        if pos.qty <= 1e-12:
            pos.status, pos.exit_reason, pos.closed_ms = "closed", reason, ts_ms
            self.closed.append(pos)
            del self.positions[pos.id]

    def close(self, pos, price, ts_ms, reason):
        if pos.status != "open":
            return
        self._fill(pos, pos.qty, price * (1 - pos.side * self.slip), self.taker, ts_ms, reason)

    def on_bar(self, symbol, ts_ms, o, h, l, c):
        """마감된 봉 하나로 손절·1차 익절·청산을 처리합니다. 이 봉 이전에 열린 포지션만 대상입니다."""
        for pos in list(self.open_positions()):
            if pos.symbol != symbol or pos.entry_bar_ts >= ts_ms:
                continue
            s = pos.side
            adverse = l if s > 0 else h
            favor = h if s > 0 else l
            if s * (o - pos.liq_price) <= 0:                         # 시가가 청산가를 넘은 갭
                lost = pos.margin * pos.qty / pos.init_qty
                self.cash -= lost
                pos.realized -= lost
                pos.qty = 0.0
                pos.status, pos.exit_reason, pos.closed_ms = "closed", "강제 청산", ts_ms
                self._emit("liquidation", pos, ts_ms, price=pos.liq_price, loss=lost)
                self.closed.append(pos)
                del self.positions[pos.id]
                continue
            if s * (adverse - pos.stop_price) <= 0:
                px = min(o, pos.stop_price) if s > 0 else max(o, pos.stop_price)
                self._fill(pos, pos.qty, px * (1 - s * self.slip), self.taker, ts_ms, "본전" if pos.tp1_done else "손절")
                continue
            if not pos.tp1_done and pos.tp1_qty > 0 and s * (favor - pos.tp1_price) >= 0:
                px = max(o, pos.tp1_price) if s > 0 else min(o, pos.tp1_price)
                pos.tp1_done = True
                self._fill(pos, pos.tp1_qty, px, self.maker, ts_ms, "1차 익절")

    # ── 재시작 시 이어서 쓰기 ───────────────────────────────────────────────
    def save_state(self):
        return {"cash": self.cash, "next": self._next, "positions": [asdict(p) for p in self.open_positions()]}

    def load_state(self, st):
        if not st:
            return
        self.cash = st["cash"]
        self._next = st["next"]
        self.positions = {p["id"]: Position(**p) for p in st["positions"]}
