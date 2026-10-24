"""위험 점검: 사례 검증에서 나온 규칙이 지켜지는지."""
import math

import pytest
from conftest import spec

from ppp2.config import ExitCfg, RiskCfg
from ppp2.engine.models import Position, Signal
from ppp2.engine.risk import AccountContext, RiskEngine

NOW = 1_800_000_000_000


def ctx(price, sp, **kw):
    base = dict(equity=10_000.0, free=10_000.0, now_ms=NOW, price=price, spec=sp, data_age_s=5)
    base.update(kw)
    return AccountContext(**base)


def sig(price, stop, side=1, l1=None, **kw):
    return Signal(symbol=kw.pop("symbol", "BTC"), side=side, price=price, ts_ms=kw.pop("ts_ms", NOW), stop=stop,
                  l1=side if l1 is None else l1, **kw)


def engine(**kw):
    return RiskEngine(RiskCfg(**kw), ExitCfg())


def failed(d):
    return {c.code for c in d.failed}


def test_lsk_case_leverage_is_capped_so_stop_comes_first():
    # 사례 06 LSK: 진입 0.13888, 손절 기준(일봉 EMA240) 0.12723 → 8.39%. 문서는 50배 → 청산 거리 1%
    d = engine(lev_cap=50, max_stop_pct=10).check(sig(0.13888, 0.12723, symbol="LSK"),
                                                   ctx(0.13888, spec("LSK", mmr=0.01, cs=1, tick=0.00001)))
    assert d.approved, d.summary()
    assert d.plan.leverage <= math.floor(0.8 / (0.0839 + 0.01))          # 8배 이하
    assert d.plan.liq_price < d.plan.stop_price                          # 롱: 청산가가 손절가보다 아래


def test_robo_case_47x_not_allowed():
    # 사례 08 ROBO: 손절 1.37%, 문서 47배 → 청산 1.13%. 허용 최대는 33배
    d = engine(lev_cap=50).check(sig(0.01902, 0.01902 * (1 - 0.0137), symbol="ROBO"),
                                 ctx(0.01902, spec("ROBO", cs=1, tick=0.00001)))
    assert d.approved
    assert d.plan.leverage <= 33
    assert d.plan.stop_dist + d.plan.mmr <= 0.8 / d.plan.leverage + 1e-9


def test_risk_amount_is_one_percent_including_fees():
    d = engine().check(sig(80_000, 79_200), ctx(80_000, spec()))
    assert d.approved
    assert d.plan.risk_amount <= 10_000 * 0.01 + 1e-6
    assert d.plan.risk_amount >= 10_000 * 0.01 * 0.9        # 계약 단위 내림으로 조금 작을 수 있음


def test_stop_on_wrong_side_rejected():
    d = engine().check(sig(80_000, 80_500), ctx(80_000, spec()))
    assert not d.approved and "STOP" in failed(d)
    d = engine().check(sig(80_000, 79_500, side=-1), ctx(80_000, spec()))
    assert not d.approved and "STOP" in failed(d)


@pytest.mark.parametrize("kw,code", [
    (dict(ts_ms=NOW - 600_000), "SIGNAL_AGE"),
    (dict(l1=0), "L1"),
    (dict(l1=-1), "L1"),
])
def test_signal_level_rejections(kw, code):
    d = engine().check(sig(80_000, 79_200, **kw), ctx(80_000, spec()))
    assert code in failed(d)


@pytest.mark.parametrize("ckw,code", [
    (dict(data_age_s=3600), "DATA_AGE"),
    (dict(price=81_000), "PRICE_DEV"),
    (dict(halted=True, halt_reason="test"), "HALT"),
    (dict(day_start_equity=10_300.0), "DAILY_LOSS"),
    (dict(week_start_equity=10_600.0), "WEEKLY_LOSS"),
    (dict(spec=None), "CONTRACT"),
])
def test_account_level_rejections(ckw, code):
    c = ctx(80_000, spec())
    for k, v in ckw.items():
        setattr(c, k, v)
    d = engine().check(sig(80_000, 79_200), c)
    assert code in failed(d)


def _pos(symbol, notional=1000.0):
    return Position(id=1, symbol=symbol, side=1, entry_price=100.0, qty=notional / 100, init_qty=notional / 100, leverage=2,
                    margin=notional / 2, stop_price=99.0, tp1_price=101.0, tp1_qty=0, liq_price=50.0, opened_ms=0, tf="15m",
                    entry_bar_ts=0)


def test_max_open_and_duplicate():
    opens = [_pos("ETH"), _pos("SOL"), _pos("BTC")]
    d = engine().check(sig(80_000, 79_200), ctx(80_000, spec(), open_positions=opens))
    assert {"MAX_OPEN", "DUPLICATE"} <= failed(d)


def test_exposure_limit_scales_or_blocks():
    opens = [_pos("ETH", 30_000.0)]                   # 이미 계좌 × 3
    d = engine().check(sig(80_000, 79_200), ctx(80_000, spec(), open_positions=opens))
    assert "EXPOSURE" in failed(d) or "SIZE" in failed(d)


def test_too_tight_or_too_wide_stop():
    assert "STOP_RANGE" in failed(engine().check(sig(80_000, 79_990), ctx(80_000, spec())))
    assert "STOP_RANGE" in failed(engine().check(sig(80_000, 70_000), ctx(80_000, spec())))


def test_cost_gate():
    # 손절 0.2% → 1차 익절 0.2% < 왕복 0.15% × 3
    d = engine().check(sig(80_000, 79_840), ctx(80_000, spec()))
    assert "COST" in failed(d)


def test_below_min_size():
    d = engine().check(sig(80_000, 79_200), ctx(80_000, spec(cs=1.0), equity=100.0, free=100.0))
    assert "SIZE" in failed(d)


def test_stop_rounded_away_from_entry():
    d = engine().check(sig(80_000, 79_200.07), ctx(80_000, spec(tick=0.1)))
    assert d.plan.stop_price == pytest.approx(79_200.0)
    d = engine().check(sig(80_000, 80_800.03, side=-1), ctx(80_000, spec(tick=0.1)))
    assert d.plan.stop_price == pytest.approx(80_800.1)


def test_all_checks_reported_at_once():
    d = engine().check(sig(80_000, 79_200, ts_ms=NOW - 600_000, l1=0), ctx(80_000, spec(), data_age_s=9999))
    assert {"SIGNAL_AGE", "L1", "DATA_AGE"} <= failed(d)
