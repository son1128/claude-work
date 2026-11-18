"""The quant agents of the desk.

The Instagram post's six "agents" map onto ordinary steps of a trading loop:

  SPOTTER -> desk.Desk.discover      find live BTC Up/Down windows
  PRIOR   -> PriorAgent              fair P(UP) from spot, vol, trend, time left
  EDGE    -> EdgeAgent               fair value minus (ask + taker fee)
  KELLY   -> KellyAgent              fractional-Kelly stake, capped
  TAKER   -> execution.*Executor     cross the spread (paper or live)
  CLOSER  -> desk.Desk.settle_due    settle expired positions, book PnL

Grok sits on top as a veto-only supervisor (see grok.py).
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Sequence

from .feeds.polymarket import Book, UpDownMarket
from .feeds.spot import Tick
from .quant import PricerInputs, kelly_fraction, linear_regression, prob_up, realized_vol_per_sec


@dataclass(frozen=True)
class Signal:
    p_up: float
    p_up_no_drift: float
    spot: float
    open_price: float
    seconds_left: float
    vol_per_sec: float
    slope_per_sec: float  # raw regression slope of log price
    r2: float
    drift_used: float  # slope after R² gate, shrink and cap
    n_ticks: int
    stale_seconds: float

    def to_dict(self) -> dict:
        return {k: (round(v, 8) if isinstance(v, float) else v) for k, v in asdict(self).items()}


@dataclass
class PriorConfig:
    vol_lookback_s: int = 300
    trend_lookback_s: int = 90
    r2_min: float = 0.35  # trend must explain >= 35% of variance to count
    # Shrink the fitted drift toward zero. Default OFF: replaying 96 15m windows
    # (Nov 2026) the regression-slope drift made Brier *worse* at every
    # checkpoint (e.g. T-300s 0.171 vs 0.106 without it). Re-test with
    # `python -m grokdesk calibrate --drift-weight 0.5` before turning it on.
    drift_weight: float = 0.0
    max_drift_bps_per_min: float = 5.0
    basis_bps: float = 2.0
    min_ticks: int = 60
    max_stale_s: float = 5.0
    vol_floor_bps_per_min: float = 1.0


class PriorAgent:
    def __init__(self, cfg: PriorConfig):
        self.cfg = cfg

    def price(
        self, market: UpDownMarket, open_price: float, ticks: Sequence[Tick], now: float
    ) -> Signal | None:
        c = self.cfg
        if len(ticks) < c.min_ticks:
            return None
        last = ticks[-1]
        stale = now - last.ts
        if stale > c.max_stale_s:
            return None

        vol_ticks = [t for t in ticks if t.ts >= now - c.vol_lookback_s]
        sigma = realized_vol_per_sec([t.ts for t in vol_ticks], [t.price for t in vol_ticks])
        sigma = max(sigma, c.vol_floor_bps_per_min / 10_000.0 / math.sqrt(60.0))

        tr = [t for t in ticks if t.ts >= now - c.trend_lookback_s]
        reg = linear_regression([t.ts - now for t in tr], [math.log(t.price) for t in tr])
        drift = 0.0
        if reg.r2 >= c.r2_min:
            cap = c.max_drift_bps_per_min / 10_000.0 / 60.0
            drift = max(-cap, min(cap, reg.slope * c.drift_weight))

        seconds_left = market.end_ts - now
        twap_s = float(market.twap_seconds or 0)
        realized = None
        if twap_s and seconds_left < twap_s:
            inside = [t.price for t in ticks if t.ts >= market.end_ts - twap_s]
            realized = sum(inside) / len(inside) if inside else None

        base = dict(
            spot=last.price,
            open_price=open_price,
            seconds_left=seconds_left,
            vol_per_sec=sigma,
            twap_seconds=twap_s or 1e-9,
            realized_twap=realized,
            basis_bps=c.basis_bps,
        )
        return Signal(
            p_up=prob_up(PricerInputs(drift_per_sec=drift, **base)),
            p_up_no_drift=prob_up(PricerInputs(drift_per_sec=0.0, **base)),
            spot=last.price,
            open_price=open_price,
            seconds_left=seconds_left,
            vol_per_sec=sigma,
            slope_per_sec=reg.slope,
            r2=reg.r2,
            drift_used=drift,
            n_ticks=len(ticks),
            stale_seconds=stale,
        )


@dataclass(frozen=True)
class Opportunity:
    side: str  # "UP" | "DOWN"
    token_id: str
    p_win: float
    best_ask: float
    limit_price: float  # worst price we accept (edge is computed here)
    fee_per_share: float
    cost_per_share: float
    edge_per_share: float  # p_win - cost_per_share
    depth_shares: float  # shares available up to limit_price

    def to_dict(self) -> dict:
        return {k: (round(v, 6) if isinstance(v, float) else v) for k, v in asdict(self).items()}


@dataclass
class EdgeConfig:
    min_edge: float = 0.04  # need >= 4¢/share of EV after fees
    slippage_ticks: int = 1
    min_price: float = 0.05
    max_price: float = 0.95
    max_model_market_gap: float = 0.30  # bigger gaps are usually our bug, not alpha


class EdgeAgent:
    def __init__(self, cfg: EdgeConfig):
        self.cfg = cfg

    def evaluate(
        self, market: UpDownMarket, sig: Signal, up_book: Book, down_book: Book
    ) -> tuple[Opportunity | None, list[dict]]:
        """Best opportunity (or None) plus a per-side breakdown for the trace."""
        c = self.cfg
        cands, notes = [], []
        for side, token, p_win, book in (
            ("UP", market.up_token, sig.p_up, up_book),
            ("DOWN", market.down_token, 1.0 - sig.p_up, down_book),
        ):
            ask = book.best_ask
            if ask is None:
                notes.append({"side": side, "skip": "no asks"})
                continue
            limit = round(min(0.99, ask + c.slippage_ticks * market.tick_size), 4)
            fee = market.fee.per_share(limit)
            cost = limit + fee
            edge = p_win - cost
            note = {"side": side, "ask": ask, "limit": limit, "p_win": round(p_win, 4),
                    "fee": round(fee, 4), "edge": round(edge, 4)}
            if not c.min_price <= ask <= c.max_price:
                note["skip"] = "price out of band"
            elif abs(p_win - ask) > c.max_model_market_gap:
                note["skip"] = "model/market gap too large"
            elif edge < c.min_edge:
                note["skip"] = "edge below threshold"
            else:
                cands.append(Opportunity(side, token, p_win, ask, limit, fee, cost, edge,
                                         book.ask_depth(limit)))
            notes.append(note)
        best = max(cands, key=lambda o: o.edge_per_share, default=None)
        return best, notes


@dataclass
class KellyConfig:
    kelly_multiplier: float = 0.25  # quarter-Kelly
    max_bankroll_fraction: float = 0.02  # never more than 2% of bankroll per trade
    max_stake_usd: float = 25.0
    max_depth_fraction: float = 0.25  # take at most 25% of visible depth


@dataclass(frozen=True)
class Sizing:
    stake_usd: float
    shares: float
    full_kelly: float
    reason: str


class KellyAgent:
    def __init__(self, cfg: KellyConfig):
        self.cfg = cfg

    def size(self, opp: Opportunity, bankroll: float, min_shares: float) -> Sizing:
        c = self.cfg
        f = kelly_fraction(opp.p_win, opp.cost_per_share)
        stake = bankroll * min(f * c.kelly_multiplier, c.max_bankroll_fraction)
        stake = min(stake, c.max_stake_usd)
        shares = stake / opp.cost_per_share if opp.cost_per_share > 0 else 0.0
        shares = min(shares, opp.depth_shares * c.max_depth_fraction)
        shares = math.floor(shares * 100) / 100  # CLOB sizes are 2-decimal
        if shares < min_shares:
            return Sizing(0.0, 0.0, f, f"below min size {min_shares:g}")
        return Sizing(round(shares * opp.cost_per_share, 4), shares, f, "ok")
