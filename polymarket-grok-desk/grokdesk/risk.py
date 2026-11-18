"""Hard limits that no agent (or Grok) can override."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class RiskConfig:
    max_daily_loss_usd: float = 50.0
    max_open_positions: int = 3
    max_positions_per_market: int = 1
    min_seconds_left: float = 20.0  # too close to the bell: feed basis dominates
    max_seconds_left: float = 600.0  # too early: little information yet
    market_cooldown_s: float = 30.0
    kill_switch_file: str = "STOP"  # touch this file to halt new orders


@dataclass(frozen=True)
class RiskState:
    realized_pnl_today: float
    open_positions: int
    open_in_market: int
    seconds_since_last_trade_in_market: float | None


class RiskManager:
    def __init__(self, cfg: RiskConfig):
        self.cfg = cfg

    def check(self, seconds_left: float, st: RiskState) -> str | None:
        """None if allowed, otherwise the reason for blocking."""
        c = self.cfg
        if c.kill_switch_file and os.path.exists(c.kill_switch_file):
            return f"kill switch file '{c.kill_switch_file}' present"
        if st.realized_pnl_today <= -abs(c.max_daily_loss_usd):
            return "daily loss limit hit"
        if st.open_positions >= c.max_open_positions:
            return "max open positions"
        if st.open_in_market >= c.max_positions_per_market:
            return "already positioned in this market"
        if seconds_left < c.min_seconds_left:
            return "too close to expiry"
        if seconds_left > c.max_seconds_left:
            return "too early in window"
        s = st.seconds_since_last_trade_in_market
        if s is not None and s < c.market_cooldown_s:
            return "market cooldown"
        return None
