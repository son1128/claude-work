"""Positions, PnL, and THE TRACE (an append-only JSONL log of every step)."""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable


@dataclass
class Position:
    id: str
    mode: str
    slug: str
    side: str  # UP | DOWN
    token_id: str
    shares: float
    avg_price: float
    fee_usd: float
    opened_ts: float
    end_ts: float
    open_price: float
    p_win: float
    status: str = "open"  # open | won | lost
    payout_usd: float = 0.0
    pnl_usd: float = 0.0
    settled_ts: float | None = None
    settled_by: str = ""  # "gamma" | "spot_proxy"

    @property
    def cost_usd(self) -> float:
        return self.shares * self.avg_price + self.fee_usd


@dataclass
class Ledger:
    path: str  # directory
    starting_bankroll: float
    clock: Callable[[], float] = time.time
    positions: list[Position] = field(default_factory=list)

    def __post_init__(self) -> None:
        os.makedirs(self.path, exist_ok=True)
        self._state = os.path.join(self.path, "positions.json")
        self._trace = os.path.join(self.path, "trace.jsonl")
        try:
            with open(self._state, encoding="utf-8") as f:
                self.positions = [Position(**p) for p in json.load(f)]
        except (OSError, ValueError, TypeError):
            self.positions = []

    # -- persistence -------------------------------------------------------
    def save(self) -> None:
        tmp = self._state + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump([asdict(p) for p in self.positions], f, indent=1)
        os.replace(tmp, self._state)

    def trace(self, kind: str, **data: Any) -> None:
        rec = {"ts": round(self.clock(), 3), "kind": kind, **data}
        with open(self._trace, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, default=str) + "\n")

    # -- state -------------------------------------------------------------
    def open_positions(self) -> list[Position]:
        return [p for p in self.positions if p.status == "open"]

    def bankroll(self) -> float:
        """Cash: start - cost of every position + payout of settled ones."""
        return self.starting_bankroll + sum(
            (p.payout_usd if p.status != "open" else 0.0) - p.cost_usd for p in self.positions
        )

    def realized_pnl_since(self, ts: float) -> float:
        return sum(p.pnl_usd for p in self.positions if p.settled_ts and p.settled_ts >= ts)

    def last_trade_ts(self, slug: str) -> float | None:
        ts = [p.opened_ts for p in self.positions if p.slug == slug]
        return max(ts) if ts else None

    def add(self, pos: Position) -> None:
        self.positions.append(pos)
        self.save()

    def settle(self, pos: Position, up_won: bool, by: str) -> None:
        won = (pos.side == "UP") == up_won
        pos.status = "won" if won else "lost"
        pos.payout_usd = pos.shares if won else 0.0
        pos.pnl_usd = round(pos.payout_usd - pos.cost_usd, 6)
        pos.settled_ts = self.clock()
        pos.settled_by = by
        self.save()

    def stats(self) -> dict:
        done = [p for p in self.positions if p.status != "open"]
        wins = sum(1 for p in done if p.status == "won")
        pnl = sum(p.pnl_usd for p in done)
        staked = sum(p.cost_usd for p in done)
        exp_pnl = sum(p.shares * p.p_win - p.cost_usd for p in done)
        return {
            "trades": len(self.positions),
            "settled": len(done),
            "open": len(self.positions) - len(done),
            "win_rate": round(wins / len(done), 4) if done else None,
            "realized_pnl": round(pnl, 2),
            "model_expected_pnl": round(exp_pnl, 2),
            "roi_on_staked": round(pnl / staked, 4) if staked else None,
            "fees_paid": round(sum(p.fee_usd for p in self.positions), 2),
            "bankroll": round(self.bankroll(), 2),
        }
