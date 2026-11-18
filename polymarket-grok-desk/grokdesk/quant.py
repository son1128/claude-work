"""Pure math: regression slope / R², realized volatility, and the P(UP) model.

Nothing here touches the network or the clock, so it is fully unit-tested.

Resolution model (verified against live Gamma market metadata, Nov 2026):
BTC "Up or Down" markets resolve UP when the Chainlink BTC/USD *60-second TWAP*
at the end of the window is >= the price at the start of the window
(``cryptoMarketConfig.twapLookbackSeconds == 60``). We approximate Chainlink
with a CEX spot feed, so a configurable ``basis_bps`` term is added to the
variance to account for feed disagreement.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


@dataclass(frozen=True)
class Regression:
    slope: float  # change in y per unit x
    intercept: float
    r2: float  # 0..1; 0 when y is constant or n < 3
    n: int


def linear_regression(xs: Sequence[float], ys: Sequence[float]) -> Regression:
    n = len(xs)
    if n != len(ys):
        raise ValueError("xs and ys must have the same length")
    if n < 3:
        return Regression(0.0, ys[-1] if ys else 0.0, 0.0, n)
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    if sxx == 0:
        return Regression(0.0, my, 0.0, n)
    slope = sxy / sxx
    intercept = my - slope * mx
    r2 = 0.0 if syy == 0 else max(0.0, min(1.0, (sxy * sxy) / (sxx * syy)))
    return Regression(slope, intercept, r2, n)


def log_returns(prices: Sequence[float]) -> list[float]:
    return [math.log(b / a) for a, b in zip(prices, prices[1:]) if a > 0 and b > 0]


def realized_vol_per_sec(times: Sequence[float], prices: Sequence[float]) -> float:
    """Volatility of log price per sqrt(second), robust to uneven sampling."""
    pairs = [
        (math.log(p1 / p0), t1 - t0)
        for (t0, p0), (t1, p1) in zip(zip(times, prices), zip(times[1:], prices[1:]))
        if t1 > t0 and p0 > 0 and p1 > 0
    ]
    if len(pairs) < 2:
        return 0.0
    total_dt = sum(dt for _, dt in pairs)
    # Sum of squared returns / elapsed time -> variance per second (zero-mean est.)
    var = sum(r * r for r, _ in pairs) / total_dt
    return math.sqrt(var)


@dataclass(frozen=True)
class PricerInputs:
    spot: float  # current proxy price
    open_price: float  # the window's reference ("price to beat")
    seconds_left: float
    vol_per_sec: float  # sigma of log price per sqrt(second)
    drift_per_sec: float = 0.0  # mu of log price per second (already shrunk)
    twap_seconds: float = 60.0
    realized_twap: float | None = None  # avg of the part of the TWAP window already elapsed
    basis_bps: float = 2.0  # extra 1-sigma uncertainty between our feed and Chainlink


def prob_up(inp: PricerInputs) -> float:
    """P(end-of-window TWAP >= open_price), clipped to [0.01, 0.99]."""
    s, k, tau, sig, mu, L = (
        inp.spot,
        inp.open_price,
        max(0.0, inp.seconds_left),
        max(0.0, inp.vol_per_sec),
        inp.drift_per_sec,
        max(1e-9, inp.twap_seconds),
    )
    if s <= 0 or k <= 0:
        raise ValueError("prices must be positive")
    basis = inp.basis_bps / 10_000.0

    if tau > L:
        # Average over [T-L, T] of a Brownian log price starting at tau seconds
        # before T: mean shift mu*(tau - L/2), variance sigma²((tau-L) + L/3).
        mean = math.log(s / k) + mu * (tau - L / 2.0)
        var = sig * sig * ((tau - L) + L / 3.0) + basis * basis
        if var <= 0:
            return _clip(1.0 if mean >= 0 else 0.0)
        return _clip(norm_cdf(mean / math.sqrt(var)))

    # Inside the TWAP window: part of the average is already fixed.
    realized = inp.realized_twap if inp.realized_twap is not None else s
    elapsed = L - tau
    future_mean = s * (1.0 + mu * tau / 2.0)
    mean_avg = (elapsed * realized + tau * future_mean) / L
    sd = math.sqrt((s * sig) ** 2 * tau**3 / 3.0) / L
    sd = math.sqrt(sd * sd + (k * basis) ** 2)
    if sd <= 0:
        return _clip(1.0 if mean_avg >= k else 0.0)
    return _clip(norm_cdf((mean_avg - k) / sd))


def _clip(p: float, lo: float = 0.01, hi: float = 0.99) -> float:
    return max(lo, min(hi, p))


# --- Fees -------------------------------------------------------------------


@dataclass(frozen=True)
class FeeModel:
    """Taker fee per share = rate * (p * (1 - p)) ** exponent.

    Read from the market's ``feeSchedule`` ({"rate", "exponent", "takerOnly"}).
    This is our reading of the schedule, peaking at p = 0.5; verify against
    Polymarket's current fee docs before trusting PnL numbers.
    """

    rate: float = 0.07
    exponent: float = 1.0
    enabled: bool = True

    def per_share(self, price: float) -> float:
        if not self.enabled:
            return 0.0
        return self.rate * (price * (1.0 - price)) ** self.exponent


# --- Sizing -----------------------------------------------------------------


def kelly_fraction(p_win: float, cost_per_share: float) -> float:
    """Full-Kelly fraction for a share costing ``cost`` that pays $1 on a win."""
    if not 0 < cost_per_share < 1:
        return 0.0
    f = (p_win - cost_per_share) / (1.0 - cost_per_share)
    return max(0.0, f)
