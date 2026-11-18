"""Does the PRIOR agent's P(UP) actually carry information?

Replays past windows from Binance 1-second candles and, at several checkpoints
before expiry, compares the model's probability with how the window resolved
(using our spot-TWAP proxy). Reports Brier score and log loss against two
baselines: a coin flip (0.5) and the same model with the regression drift
switched off — so you can see whether "regression slope + R² filter" (the
post's claimed secret sauce) adds anything beyond plain distance-to-strike.

This measures *calibration*, not profit: it has no historical order books, so
it cannot tell you whether the market already priced the same information.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .agents import PriorAgent, PriorConfig
from .feeds.polymarket import DURATIONS, FeeModel, UpDownMarket
from .feeds.spot import SpotFeed, Tick


@dataclass
class Score:
    n: int = 0
    brier: float = 0.0
    brier_nodrift: float = 0.0
    logloss: float = 0.0
    hits: int = 0
    probs: list[float] = field(default_factory=list)

    def add(self, p: float, p0: float, y: int) -> None:
        self.n += 1
        self.brier += (p - y) ** 2
        self.brier_nodrift += (p0 - y) ** 2
        self.logloss += -(y * math.log(p) + (1 - y) * math.log(1 - p))
        self.hits += int((p >= 0.5) == bool(y))
        self.probs.append(p)

    def row(self) -> dict:
        if not self.n:
            return {"n": 0}
        return {
            "n": self.n,
            "brier": round(self.brier / self.n, 4),
            "brier_no_drift": round(self.brier_nodrift / self.n, 4),
            "brier_coinflip": 0.25,
            "log_loss": round(self.logloss / self.n, 4),
            "hit_rate": round(self.hits / self.n, 3),
        }


def run(
    spot: SpotFeed,
    end_before: int,
    windows: int,
    duration: str = "15m",
    checkpoints: tuple[int, ...] = (600, 300, 120, 60, 30),
    prior_cfg: PriorConfig | None = None,
    twap_seconds: int = 60,
) -> dict[int, dict]:
    step = DURATIONS[duration]
    agent = PriorAgent(prior_cfg or PriorConfig(drift_weight=0.5))
    lookback = max(agent.cfg.vol_lookback_s, agent.cfg.trend_lookback_s)
    scores = {c: Score() for c in checkpoints if c < step}
    last_start = end_before - end_before % step - step
    for i in range(windows):
        start = last_start - i * step
        end = start + step
        rows = spot.klines_1s(start - lookback, step + lookback)
        ticks = [Tick(ts, p) for ts, p in rows]
        if len(ticks) < (step + lookback) * 0.9:
            continue  # gappy data; skip rather than score noise
        k = next((p for ts, p in rows if ts >= start + 1), None)
        tail = [p for ts, p in rows if end - twap_seconds < ts <= end]
        if k is None or not tail:
            continue
        y = int(sum(tail) / len(tail) >= k)
        m = UpDownMarket(f"replay-{start}", "", "", "u", "d", start, end, duration,
                         twap_seconds, 5, 0.01, FeeModel(), True, False)
        for c, sc in scores.items():
            now = end - c
            visible = [t for t in ticks if t.ts <= now]
            sig = agent.price(m, k, visible, now)
            if sig is not None:
                sc.add(sig.p_up, sig.p_up_no_drift, y)
    return {c: s.row() for c, s in scores.items()}
