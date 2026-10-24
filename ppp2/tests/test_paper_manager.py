"""모의 체결과 청산 관리."""
import pytest
from conftest import spec

from ppp2.config import ExitCfg, RiskCfg
from ppp2.engine.manager import ExitManager
from ppp2.engine.models import Signal
from ppp2.engine.paper import PaperBroker
from ppp2.engine.risk import AccountContext, RiskEngine

NOW = 1_800_000_000_000
STEP = 900_000


def opened(side=1, entry=100.0, stop=98.0, slip=0.0, tp1_frac=0.5):
    sp = spec("X", cs=1.0, tick=0.01, step=1.0, min_qty=1, mmr=0.01)
    d = RiskEngine(RiskCfg(), ExitCfg(tp1_frac=tp1_frac)).check(
        Signal("X", side, entry, NOW, stop=stop, l1=side), AccountContext(10_000, 10_000, NOW, entry, sp))
    assert d.approved, d.summary()
    b = PaperBroker(equity=10_000, taker_fee=0.00075, maker_fee=-0.0001, slippage_bps=slip)
    pos = b.open(d.plan, NOW, "15m", NOW, 1.0)
    return b, pos, d.plan


def test_stop_loss_costs_about_one_percent():
    b, pos, plan = opened()
    b.on_bar("X", NOW + STEP, 100, 100.5, 97.5, 98.2)
    assert pos.status == "closed" and pos.exit_reason == "손절"
    assert b.cash - 10_000 == pytest.approx(-plan.risk_amount, rel=0.02)


def test_bar_before_entry_is_ignored():
    b, pos, _ = opened()
    b.on_bar("X", NOW, 100, 100.5, 90, 95)              # 진입 봉 자체
    assert pos.status == "open"


def test_same_bar_stop_and_tp1_takes_stop():
    b, pos, _ = opened()
    b.on_bar("X", NOW + STEP, 100, 103, 97, 100)
    assert pos.status == "closed" and pos.exit_reason == "손절"


def test_tp1_then_manager_moves_stop_to_breakeven_and_trails():
    b, pos, plan = opened()
    m = ExitManager(ExitCfg())
    b.on_bar("X", NOW + STEP, 100, 102.5, 99.5, 102.2)
    assert pos.tp1_done and pos.qty == plan.qty - plan.tp1_qty
    m.on_bar_close(pos, (NOW + STEP, 100, 102.5, 99.5, 102.2), 101.0, b, NOW + 2 * STEP)
    assert pos.stop_price == pytest.approx(pos.entry_price)
    m.on_bar_close(pos, (NOW + 2 * STEP, 102, 102, 100.5, 100.8), 101.0, b, NOW + 3 * STEP)
    assert pos.status == "closed" and pos.exit_reason == "추적 청산"


def test_breakeven_stop_after_tp1():
    b, pos, _ = opened()
    m = ExitManager(ExitCfg())
    b.on_bar("X", NOW + STEP, 100, 102.5, 99.5, 102.2)
    m.on_bar_close(pos, (NOW + STEP, 100, 102.5, 99.5, 102.2), 99.0, b, NOW + 2 * STEP)
    b.on_bar("X", NOW + 2 * STEP, 101, 101, 99.8, 100.2)
    assert pos.status == "closed" and pos.exit_reason == "본전"
    assert pos.realized > 0                                 # 1차 익절 이익이 남음


def test_time_stop():
    b, pos, _ = opened()
    m = ExitManager(ExitCfg(pre_tp_bars=3))
    m.on_bar_close(pos, (NOW + 3 * STEP, 100, 100.5, 99.5, 100.1), 99.0, b, NOW + 4 * STEP)
    assert pos.status == "closed" and pos.exit_reason == "시간"


def test_gap_through_liquidation():
    b, pos, _ = opened()
    b.on_bar("X", NOW + STEP, pos.liq_price * 0.9, pos.liq_price * 0.9, pos.liq_price * 0.8, pos.liq_price * 0.85)
    assert pos.status == "closed" and pos.exit_reason == "강제 청산"
    assert 10_000 - b.cash == pytest.approx(pos.margin + pos.fees, rel=0.01)


def test_short_side_mirror():
    b, pos, _ = opened(side=-1, entry=100.0, stop=102.0)
    b.on_bar("X", NOW + STEP, 100, 100.5, 97.5, 97.9)
    assert pos.tp1_done


def test_state_roundtrip():
    b, pos, _ = opened()
    st = b.save_state()
    b2 = PaperBroker()
    b2.load_state(st)
    assert b2.cash == b.cash and b2.open_positions()[0].stop_price == pos.stop_price
