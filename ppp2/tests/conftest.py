import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ppp2.data.exchanges import INTERVAL_MS, ContractSpec  # noqa: E402
from ppp2.data.store import Store  # noqa: E402


class FakeHttp:
    """경로별 응답 함수를 등록해 두고 호출 기록을 남기는 가짜 HTTP."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, path, params=None, headers=None):
        return self.request("GET", path, params=params, headers=headers)

    def request(self, method, path, params=None, body=None, headers=None):
        self.calls.append((method, path, params, body, headers))
        for key, fn in self.routes.items():
            m, p = key
            if m == method and (path == p or (p.endswith("*") and path.startswith(p[:-1]))):
                return fn(params, body)
        raise AssertionError(f"예상하지 못한 호출 {method} {path}")


def spec(symbol="BTC", mmr=0.01, max_lev=50, cs=0.0001, tick=0.1, taker=0.00075, min_qty=1, step=1.0, ex="gate"):
    return ContractSpec(exchange=ex, symbol=symbol, contract_size=cs, min_qty=min_qty, qty_step=step, price_tick=tick,
                        mmr=mmr, max_leverage=max_lev, maker_fee=-0.0001, taker_fee=taker)


def synth(n, interval, start_ms=1_700_000_000_000, seed=1, drift=0.0, vol=0.004, p0=100.0):
    """기하 랜덤워크 봉. drift > 0이면 상승 추세."""
    rng = np.random.default_rng(seed)
    step = INTERVAL_MS[interval]
    start_ms = start_ms // step * step
    r = rng.normal(drift, vol, n)
    close = p0 * np.exp(np.cumsum(r))
    open_ = np.concatenate([[p0], close[:-1]])
    hi = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, vol / 2, n)))
    lo = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, vol / 2, n)))
    return [(start_ms + i * step, float(open_[i]), float(hi[i]), float(lo[i]), float(close[i]), 1.0) for i in range(n)]


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "m.db"))
    yield s
    s.close()
