"""엔진 전체 경로 (합성 시세 저장소 사용)."""
import os
import sys

import numpy as np
import pytest
from conftest import spec, synth

from ppp2.config import Config
from ppp2.core import features as fx
from ppp2.data.exchanges import INTERVAL_MS
from ppp2.engine import signals as sg
from ppp2.engine.engine import Engine
from ppp2.engine.journal import Journal
from ppp2.engine.models import Signal
from ppp2.engine.paper import PaperBroker

H4 = INTERVAL_MS["4h"]
M15 = INTERVAL_MS["15m"]


def fill_store(store, symbols=("AAA", "BBB"), n15=3000):
    end = 1_800_000_000_000 // H4 * H4
    for k, s in enumerate(symbols):
        h4 = synth(400, "4h", start_ms=end - 400 * H4, seed=10 + k, drift=0.002, vol=0.01)
        m15 = synth(n15, "15m", start_ms=end - n15 * M15, seed=20 + k, drift=0.0002, vol=0.004, p0=h4[-int(n15 / 16)][4])
        store.upsert_candles("gate", s, "4h", h4)
        store.upsert_candles("gate", s, "15m", m15)
        store.upsert_contracts([spec(s, cs=1.0, tick=0.0001, mmr=0.01)], 0)
    return end


def make(tmp_path, store, symbols=("AAA", "BBB"), **risk):
    cfg = Config()
    cfg.engine.symbols = list(symbols)
    cfg.general.state_dir = str(tmp_path)
    for k, v in risk.items():
        setattr(cfg.risk, k, v)
    j = Journal(str(tmp_path / "e.db"), "test")
    return Engine(cfg, store, PaperBroker(equity=10_000), j), j


def test_replay_invariants(tmp_path, store):
    fill_store(store)
    eng, j = make(tmp_path, store)
    res = eng.replay()
    assert res["signals"] > 20 and res["approved"] > 0
    assert res["liquidations"] == 0
    import json
    plans = [json.loads(r[0]) for r in j.db.execute("SELECT plan FROM decisions WHERE approved=1")]
    assert all(p["stop_dist"] + p["mmr"] <= 0.8 / p["leverage"] + 1e-9 for p in plans)
    assert all(p["leverage"] <= eng.cfg.risk.lev_cap for p in plans)
    # 거래 하나의 손실이 계좌의 1%를 크게 넘지 않음 (슬리피지·갭 여유 50%)
    worst = min(p.realized - p.fees for p in eng.broker.closed)
    assert worst >= -10_000 * 0.015
    # L1 반대 신호는 모두 거부
    rows = j.db.execute("SELECT s.side, s.l1, d.approved FROM signals s JOIN decisions d ON d.signal_id = s.id").fetchall()
    assert all(not ok for side, l1, ok in rows if l1 != side)


def test_step_first_run_does_not_replay_history(tmp_path, store):
    fill_store(store)
    eng, j = make(tmp_path, store)
    eng._now = lambda: 1_800_000_000_000 + 60_000
    assert eng.step() == 2                         # 종목마다 마지막 봉 하나씩만
    assert eng.step() == 0                         # 새 봉 없음


def test_halt_blocks_new_entries_but_keeps_managing(tmp_path, store):
    fill_store(store)
    eng, j = make(tmp_path, store)
    eng.halt("점검")
    s = Signal("AAA", 1, 100.0, 1_800_000_000_000, stop=98.0, l1=1)
    d = eng.submit(s, price=100.0, data_age_s=0)
    assert not d.approved and "HALT" in {c.code for c in d.failed}
    open(os.path.join(tmp_path, "HALT"), "w").close()
    eng.resume()
    assert eng.halted()[0]                         # HALT 파일이 남아 있으면 계속 정지
    os.remove(os.path.join(tmp_path, "HALT"))
    assert not eng.halted()[0]


def test_submit_without_stop_uses_structural_stop(tmp_path, store):
    end = fill_store(store)
    eng, j = make(tmp_path, store, require_l1=False, max_signal_age_s=10**9, max_data_age_s=10**9)
    df = store.load("gate", "AAA", "15m")
    px = float(df["close"].iloc[-1])
    eng._now = lambda: end
    d = eng.submit(Signal("AAA", 1, px, end, stop=None, l1=None))
    assert d.signal.stop is not None and d.signal.l1 is not None
    if d.signal.stop < px:
        assert "STOP" not in {c.code for c in d.failed}


def test_l1_series_uses_previous_closed_htf_bar():
    import pandas as pd
    h = pd.DataFrame({"time": [i * H4 for i in range(300)], "close": np.linspace(1, 3, 300)})   # 계속 상승 → 정배열
    tf_times = np.array([299 * H4, 299 * H4 + M15, 300 * H4])
    out = sg.l1_series(tf_times, h, "4h")
    assert list(out) == [1, 1, 1]
    assert sg.l1_series(tf_times, h.iloc[:100], "4h").tolist() == [0, 0, 0]     # 240봉 미만


def test_core_matches_research_code():
    """패키지 코드(ppp2/core)와 연구 코드(pine-scripts/ppp/verify)가 같은 결과를 내는지."""
    root = os.path.join(os.path.dirname(__file__), "..", "..", "pine-scripts", "ppp", "verify")
    if not os.path.isdir(root):
        pytest.skip("연구 코드 없음")
    sys.path.insert(0, root)
    import ppp_v2 as ref
    import pandas as pd
    rows = synth(4000, "15m", seed=3, drift=0.0001)
    df = pd.DataFrame(rows, columns=["time", "open", "high", "low", "close", "volume"])
    a, b = fx.features(df), ref.features(df)
    for k in ("score_l", "score_s", "near_l", "near_s", "tangle", "l1", "bull_div", "bear_div"):
        assert np.array_equal(np.nan_to_num(a[k]), np.nan_to_num(b[k])), k
    assert np.allclose(np.nan_to_num(a["osc"]["k"]), np.nan_to_num(b["osc"]["k"]))


def test_external_l1_is_recomputed(tmp_path, store):
    end = fill_store(store)
    eng, j = make(tmp_path, store, max_signal_age_s=10**9, max_data_age_s=10**9)
    df = store.load("gate", "AAA", "15m")
    px = float(df["close"].iloc[-1])
    eng._now = lambda: end
    h = store.load("gate", "AAA", "4h")
    true_l1 = int(sg.l1_series(df["time"].to_numpy()[-1:], h, "4h")[0])
    fake = 1 if true_l1 != 1 else -1
    d = eng.submit(Signal("AAA", 1, px, end, stop=px * 0.98, l1=fake, source="webhook"))
    assert d.signal.l1 == true_l1 and "알림 L1" in d.signal.source
