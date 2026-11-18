"""TAKER: turn a sized opportunity into a fill.

``PaperExecutor`` simulates a fill-or-kill buy by walking the real order book
up to the limit price and charging the market's taker fee. It is the default.

``LiveExecutor`` places real orders through Polymarket's official
``py-clob-client``. It refuses to start unless every gate in
``assert_live_allowed`` passes. It has NOT been exercised against the live
exchange in this repository — treat it as a starting point and test it with
the minimum order size first.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Protocol

from .agents import Opportunity, Sizing
from .feeds.polymarket import Book, UpDownMarket

LIVE_CONFIRM_ENV = "GROKDESK_LIVE_CONFIRM"
LIVE_CONFIRM_VALUE = "I_ACCEPT_REAL_MONEY_RISK"


@dataclass(frozen=True)
class Fill:
    shares: float
    avg_price: float
    fee_usd: float
    order_id: str

    @property
    def cost_usd(self) -> float:
        return self.shares * self.avg_price + self.fee_usd


class Executor(Protocol):
    mode: str

    def buy(self, market: UpDownMarket, opp: Opportunity, size: Sizing, book: Book) -> Fill | None: ...


class PaperExecutor:
    mode = "paper"

    def __init__(self) -> None:
        self._n = 0

    def buy(self, market: UpDownMarket, opp: Opportunity, size: Sizing, book: Book) -> Fill | None:
        want, got, notional, fee = size.shares, 0.0, 0.0, 0.0
        for lvl in book.asks:
            if lvl.price > opp.limit_price + 1e-12 or got >= want - 1e-9:
                break
            take = min(lvl.size, want - got)
            got += take
            notional += take * lvl.price
            fee += take * market.fee.per_share(lvl.price)
        if got < want - 1e-9:  # fill-or-kill: not enough depth at our limit
            return None
        self._n += 1
        return Fill(round(got, 4), notional / got, round(fee, 6), f"paper-{self._n}")


def assert_live_allowed(geo: dict, cfg_mode: str) -> None:
    """Raise unless the operator has opted in at every layer."""
    if cfg_mode != "live":
        raise PermissionError("config [desk].mode must be 'live'")
    if os.environ.get(LIVE_CONFIRM_ENV) != LIVE_CONFIRM_VALUE:
        raise PermissionError(f"set {LIVE_CONFIRM_ENV}={LIVE_CONFIRM_VALUE} to trade real money")
    if geo.get("blocked", True):
        raise PermissionError(
            f"Polymarket reports this location as restricted ({geo.get('country')}). "
            "Live trading is disabled; do not try to work around the geoblock."
        )
    for var in ("POLY_PRIVATE_KEY", "POLY_FUNDER"):
        if not os.environ.get(var):
            raise PermissionError(f"{var} is not set")


class LiveExecutor:
    mode = "live"

    def __init__(self, clob_host: str = "https://clob.polymarket.com", signature_type: int = 1):
        try:
            from py_clob_client.client import ClobClient  # type: ignore
        except ImportError as e:  # pragma: no cover - optional dependency
            raise RuntimeError("pip install -r requirements-live.txt") from e
        self.client = ClobClient(
            clob_host,
            key=os.environ["POLY_PRIVATE_KEY"],
            chain_id=137,
            signature_type=signature_type,
            funder=os.environ["POLY_FUNDER"],
        )
        self.client.set_api_creds(self.client.create_or_derive_api_creds())

    def buy(self, market: UpDownMarket, opp: Opportunity, size: Sizing, book: Book) -> Fill | None:  # pragma: no cover
        from py_clob_client.clob_types import MarketOrderArgs, OrderType  # type: ignore
        from py_clob_client.order_builder.constants import BUY  # type: ignore

        amount = round(size.shares * opp.limit_price, 2)  # USDC to spend
        order = self.client.create_market_order(
            MarketOrderArgs(token_id=opp.token_id, amount=amount, side=BUY, price=opp.limit_price)
        )
        resp = self.client.post_order(order, OrderType.FOK)
        if not resp or not resp.get("success"):
            return None
        shares = float(resp.get("takingAmount") or size.shares)
        spent = float(resp.get("makingAmount") or amount)
        avg = spent / shares if shares else opp.limit_price
        return Fill(shares, avg, shares * market.fee.per_share(avg), str(resp.get("orderID", "")))
