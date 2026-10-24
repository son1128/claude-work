"""주문 전 위험 점검 (기획서 L2).

사례 검증에서 나온 규칙을 그대로 코드로 옮겼습니다.
  - 손절이 청산보다 먼저 와야 한다:  손절 거리 + 유지증거금률 ≤ liq_safety / 레버리지
  - 레버리지는 고르는 값이 아니라 계산 결과: min(청산 안전 배수, 상한, 거래소 최대)
  - 1회 손실 = 계좌 risk_pct% (수수료 포함), 격리 마진만
  - 하루·한 주 손실 한도, 동시 포지션 수, 전체 명목가 한도, 같은 종목 중복 금지
  - 오래된 신호·오래된 시세·현재가와 동떨어진 신호 거부, L1 방향 불일치 거부
  - 1차 익절 거리가 왕복 수수료의 cost_gate_mult배보다 짧으면 거부
모든 항목을 다 평가해서 실패 이유를 한 번에 돌려줍니다.
"""
import math
from dataclasses import dataclass, field

from .models import Check, Decision, OrderPlan

EPS = 1e-9


@dataclass
class AccountContext:
    equity: float
    free: float
    now_ms: int
    price: float                        # 현재가 (시장가 진입 기준)
    spec: object                        # ContractSpec 또는 None
    open_positions: list = field(default_factory=list)
    data_age_s: float = 0.0
    day_start_equity: float | None = None
    week_start_equity: float | None = None
    halted: bool = False
    halt_reason: str = ""


def _floor_step(x, step):
    return math.floor(x / step + EPS) * step if step > 0 else x


def _round_tick(x, tick, mode):
    if tick <= 0:
        return x
    n = x / tick
    n = math.floor(n + EPS) if mode == "down" else math.ceil(n - EPS) if mode == "up" else round(n)
    return round(n * tick, 12)


class RiskEngine:
    def __init__(self, risk_cfg, exit_cfg):
        self.cfg = risk_cfg
        self.ex = exit_cfg

    def check(self, sig, ctx):
        c = self.cfg
        checks = []

        def add(code, ok, detail):
            checks.append(Check(code, bool(ok), detail))
            return ok

        add("HALT", not ctx.halted, "정지 상태: " + ctx.halt_reason if ctx.halted else "정상 운영")
        spec = ctx.spec
        add("CONTRACT", spec is not None and spec.active, "계약 정보 있음" if spec else "계약 정보 없음 (수집기 contracts 동기화 필요)")
        add("SIDE", sig.side in (1, -1), f"방향 {sig.side}")
        age = (ctx.now_ms - sig.ts_ms) / 1000
        add("SIGNAL_AGE", age <= c.max_signal_age_s, f"신호 경과 {age:.0f}초 (한도 {c.max_signal_age_s}초)")
        add("DATA_AGE", ctx.data_age_s <= c.max_data_age_s, f"시세 경과 {ctx.data_age_s:.0f}초 (한도 {c.max_data_age_s}초)")
        dev = abs(ctx.price - sig.price) / sig.price * 100 if sig.price > 0 else 999
        add("PRICE_DEV", dev <= c.max_price_dev_pct, f"신호가 대비 현재가 {dev:.2f}% (한도 {c.max_price_dev_pct}%)")
        if c.require_l1:
            add("L1", sig.l1 == sig.side, f"L1 방향 {sig.l1} / 신호 {sig.side}")
        if c.min_score > 0:
            add("SCORE", sig.score is not None and sig.score >= c.min_score, f"점수 {sig.score} (최소 {c.min_score})")
        if ctx.day_start_equity:
            dd = (ctx.day_start_equity - ctx.equity) / ctx.day_start_equity * 100
            add("DAILY_LOSS", dd < c.daily_loss_pct, f"오늘 손실 {dd:.2f}% (한도 {c.daily_loss_pct}%)")
        if ctx.week_start_equity:
            wd = (ctx.week_start_equity - ctx.equity) / ctx.week_start_equity * 100
            add("WEEKLY_LOSS", wd < c.weekly_loss_pct, f"이번 주 손실 {wd:.2f}% (한도 {c.weekly_loss_pct}%)")
        add("MAX_OPEN", len(ctx.open_positions) < c.max_open, f"보유 {len(ctx.open_positions)}개 (한도 {c.max_open})")
        add("DUPLICATE", all(p.symbol != sig.symbol for p in ctx.open_positions), "같은 종목 포지션 없음"
            if all(p.symbol != sig.symbol for p in ctx.open_positions) else "같은 종목 포지션 보유 중")
        add("MARGIN_MODE", True, "격리 마진")

        entry = ctx.price
        stop = sig.stop
        plan = None
        if stop is None or entry <= 0 or sig.side not in (1, -1):
            add("STOP", False, "손절가 없음")
            return Decision(sig, False, checks, None)
        dist = sig.side * (entry - stop) / entry
        if not add("STOP", dist > 0, f"손절가 {stop:g}가 진입가 {entry:g}의 {'아래' if sig.side > 0 else '위'}에 있어야 함"
                   if dist <= 0 else f"손절 거리 {dist*100:.2f}%"):
            return Decision(sig, False, checks, None)
        add("STOP_RANGE", c.min_stop_pct / 100 <= dist <= c.max_stop_pct / 100,
            f"손절 거리 {dist*100:.2f}% (허용 {c.min_stop_pct}~{c.max_stop_pct}%)")
        if spec is None:
            return Decision(sig, False, checks, None)

        mmr, taker = spec.mmr, max(spec.taker_fee, 0.0)
        lev_safe = math.floor(c.liq_safety / (dist + mmr) + EPS)
        lev_max = int(min(lev_safe, c.lev_cap, spec.max_leverage))
        add("LEVERAGE", lev_max >= 1, f"손절 {dist*100:.2f}% + 유지증거금 {mmr*100:.2f}% → 청산 안전 최대 {lev_safe}배, 사용 상한 {lev_max}배")
        lev_max = max(lev_max, 1)

        open_notional = sum(p.notional for p in ctx.open_positions)
        room = c.max_total_notional_x * ctx.equity - open_notional
        add("EXPOSURE", room > 0, f"전체 명목가 여유 {room:,.0f}")
        risk_amt = ctx.equity * c.risk_pct / 100
        margin_cap = ctx.equity * c.max_margin_frac
        notional = min(risk_amt / (dist + 2 * taker), max(room, 0.0), margin_cap * lev_max, max(ctx.free, 0.0) * lev_max * 0.95)
        lev = int(min(max(1, math.ceil(notional / margin_cap - EPS)), lev_max)) if margin_cap > 0 else 1
        unit = entry * spec.contract_size
        qty = _floor_step(notional / unit, spec.qty_step) if unit > 0 else 0.0
        add("SIZE", qty >= spec.min_qty - EPS, f"{qty:g}계약 (최소 {spec.min_qty:g})")
        notional = qty * unit
        margin = notional / lev
        liq_dist = 1 / lev - mmr
        liq = entry * (1 - sig.side * liq_dist)
        stop_r = _round_tick(stop, spec.price_tick, "down" if sig.side > 0 else "up")
        dist_r = sig.side * (entry - stop_r) / entry
        add("STOP_BEFORE_LIQ", dist_r + mmr <= c.liq_safety / lev + EPS and sig.side * (stop_r - liq) > 0,
            f"손절 {dist_r*100:.2f}% / 청산 {liq_dist*100:.2f}% ({lev}배)")
        tp1 = _round_tick(entry + sig.side * self.ex.tp1_r * dist_r * entry, spec.price_tick, "nearest")
        tp_dist = sig.side * (tp1 - entry) / entry
        add("COST", tp_dist >= c.cost_gate_mult * 2 * taker - EPS,
            f"1차 익절 거리 {tp_dist*100:.3f}% vs 왕복 수수료 {2*taker*100:.3f}% × {c.cost_gate_mult:g}")
        tp1_qty = _floor_step(qty * self.ex.tp1_frac, spec.qty_step)
        if tp1_qty < spec.min_qty - EPS or tp1_qty >= qty:
            tp1_qty = 0.0                                          # 나눌 수 없으면 1차 익절 생략
        plan = OrderPlan(symbol=sig.symbol, side=sig.side, entry_price=entry, qty=qty, notional=notional, leverage=lev,
                         margin=margin, stop_price=stop_r, tp1_price=tp1, tp1_qty=tp1_qty, liq_price=liq,
                         stop_dist=dist_r, liq_dist=liq_dist, risk_amount=notional * (dist_r + 2 * taker),
                         fee_est=notional * 2 * taker, mmr=mmr)
        # 거부된 경우에도 계산한 계획을 남겨 두면 기록에서 "몇 배였으면 됐는지" 볼 수 있습니다
        return Decision(sig, all(ch.ok for ch in checks), checks, plan)
