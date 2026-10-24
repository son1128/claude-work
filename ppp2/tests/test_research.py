"""단계 2 연구 모듈: 미래 데이터 미참조, 비용 계산, 상장 폐지, 변동성 비중, 기간 분할 검증."""
from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from ppp2.research import trend as tr

H4 = 14_400_000


def panel(n=3000, k=3, seed=1, vol=0.01, drift=0.0005):
    rng = np.random.default_rng(seed)
    idx = 1_600_000_000_000 // H4 * H4 + np.arange(n) * H4
    data = {f"S{i}": 100 * np.exp(np.cumsum(rng.normal(drift * (1 if i % 2 == 0 else -1), vol, n))) for i in range(k)}
    return pd.DataFrame(data, index=idx)


CFG = tr.TrendCfg(lens=(10, 20, 40), vol_days=10, port_days=20)


def test_weights_do_not_use_future_prices():
    c = panel()
    w1 = tr.apply_band(tr.target_weights(c, tr.direction(c, CFG), CFG), CFG.band)
    c2 = c.copy()
    c2.iloc[2000:] *= np.linspace(1, 3, len(c2) - 2000)[:, None]      # 2000봉 이후만 바꿈
    w2 = tr.apply_band(tr.target_weights(c2, tr.direction(c2, CFG), CFG), CFG.band)
    pd.testing.assert_frame_equal(w1.iloc[:2000], w2.iloc[:2000])


def test_return_uses_previous_bar_weight_and_costs():
    c = pd.DataFrame({"A": [100.0, 100.0, 110.0, 110.0]}, index=np.arange(4) * H4)
    w = pd.DataFrame({"A": [0.0, 0.5, 0.5, 0.0]}, index=c.index)
    cfg = replace(CFG, fee=0.001, slippage=0.0, funding_apr=0.0)
    net, parts = tr.simulate(c, w, cfg)
    assert parts["gross"].tolist() == pytest.approx([0, 0, 0.05, 0])        # 1봉에 산 0.5가 2봉 +10%
    assert parts["cost"].tolist() == pytest.approx([0, 0.0005, 0, 0.0005])  # 진입·청산 회전율 0.5씩


def test_funding_paid_by_longs_received_by_shorts():
    c = pd.DataFrame({"A": [100.0] * 4}, index=np.arange(4) * H4)
    cfg = replace(CFG, fee=0.0, slippage=0.0, funding_apr=0.219)            # 봉당 0.0001
    _, pl = tr.simulate(c, pd.DataFrame({"A": [1.0] * 4}, index=c.index), cfg)
    _, ps = tr.simulate(c, pd.DataFrame({"A": [-1.0] * 4}, index=c.index), cfg)
    assert pl["net"].iloc[2] == pytest.approx(-0.0001) and ps["net"].iloc[2] == pytest.approx(0.0001)


def test_delisted_asset_is_closed_and_ignored_after():
    c = panel(600, 2)
    c.iloc[400:, 1] = np.nan
    d = tr.direction(c, CFG)
    w = tr.target_weights(c, d, CFG)
    assert (w.iloc[400:, 1] == 0).all()
    _, parts = tr.simulate(c, w, CFG)
    assert parts["gross"].notna().all()


def test_vol_target_scales_inverse_to_volatility():
    c = panel(4000, 2, vol=0.01)
    c["S1"] = 100 * np.exp(np.cumsum(np.random.default_rng(3).normal(0, 0.02, len(c))))   # 변동성 2배
    cfg = replace(CFG, port_scale=False, band=0.0)
    d = pd.DataFrame(1.0, index=c.index, columns=c.columns)
    w = tr.target_weights(c, d, cfg).iloc[-500:]
    assert (w["S0"] / w["S1"]).median() == pytest.approx(2.0, rel=0.15)
    vol = tr.realized_vol(c, cfg.vol_days, "4h").iloc[-500:]
    assert (w["S0"] * vol["S0"]).median() == pytest.approx(cfg.target_vol / 2, rel=0.05)


def test_gross_cap():
    c = panel(2000, 3, vol=0.0005)                     # 아주 낮은 변동성 → 비중이 커지려 함
    cfg = replace(CFG, asset_cap=10, gross_cap=2.0)
    w = tr.target_weights(c, pd.DataFrame(1.0, index=c.index, columns=c.columns), cfg)
    assert w.abs().sum(axis=1).max() <= 2.0 + 1e-9


def test_band_skips_small_changes_but_not_flips():
    w = pd.DataFrame({"A": [0.5, 0.55, 0.7, -0.1, 0.0]})
    out = tr.apply_band(w, 0.2)["A"].tolist()
    assert out == [0.5, 0.5, 0.7, -0.1, 0.0]


def test_long_only_has_no_shorts():
    c = panel()
    assert (tr.direction(c, replace(CFG, long_only=True)) >= 0).all().all()


def test_walk_forward_windows_are_contiguous_and_use_chosen_series():
    c = panel(4400 * 2, 3)                            # 약 4년
    cfgs = [CFG, replace(CFG, lens=(20, 40, 80))]
    oos, windows, series = tr.walk_forward(c, cfgs, train_days=365, test_days=120)
    assert len(windows) >= 3
    for a, b in zip(windows, windows[1:]):
        assert a["test_end"] == b["test_start"]
    w0 = windows[0]
    chosen = next(cf for cf in cfgs if cf.label() == w0["chosen"])
    s = series[chosen]
    part = s[(s.index >= pd.Timestamp(w0["test_start"])) & (s.index < pd.Timestamp(w0["test_end"]))]
    assert (1 + part).prod() - 1 == pytest.approx(w0["test_return"])


def test_split_segments_on_long_gap():
    t = np.concatenate([np.arange(100), np.arange(200, 300)]) * H4
    df = pd.DataFrame({"time": t, "close": np.r_[np.full(100, 0.0001), np.full(100, 5.0)]})
    segs = tr.split_segments(df, H4)
    assert [len(x) for x in segs] == [100, 100]
    assert all(isinstance(x, pd.DataFrame) for x in segs)
