"""Polymarket read-only clients: Gamma (market discovery) and CLOB (order books).

BTC Up/Down events use deterministic slugs ``btc-updown-{5m|15m|1h|4h}-{start}``
where ``start`` is the window's start as a unix timestamp, so the "spotter"
can find the live windows without paging through every market.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..http import HttpError, Transport
from ..quant import FeeModel

DURATIONS = {"5m": 300, "15m": 900, "1h": 3600, "4h": 14400}


def parse_ts(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()


@dataclass
class UpDownMarket:
    slug: str
    question: str
    condition_id: str
    up_token: str
    down_token: str
    start_ts: float
    end_ts: float
    duration: str
    twap_seconds: int
    min_order_size: float
    tick_size: float
    fee: FeeModel
    accepting_orders: bool
    closed: bool
    outcome_prices: tuple[float, float] | None = None  # (up, down) from Gamma
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def resolved_up(self) -> bool | None:
        """True/False once Gamma shows a settled 1/0 price, else None."""
        if not self.closed or not self.outcome_prices:
            return None
        up, down = self.outcome_prices
        if up >= 0.99 and down <= 0.01:
            return True
        if down >= 0.99 and up <= 0.01:
            return False
        return None


def _loads(v: Any) -> Any:
    return json.loads(v) if isinstance(v, str) else v


def parse_market(m: dict, duration: str) -> UpDownMarket:
    outcomes = [o.lower() for o in _loads(m["outcomes"])]
    tokens = _loads(m["clobTokenIds"])
    if outcomes != ["up", "down"] or len(tokens) != 2:
        raise ValueError(f"not an Up/Down market: {m.get('slug')}")
    prices = _loads(m.get("outcomePrices") or "null")
    fs = m.get("feeSchedule") or {}
    cfg = m.get("cryptoMarketConfig") or {}
    end_ts = parse_ts(m["endDate"])
    start_raw = m.get("eventStartTime")
    start_ts = parse_ts(start_raw) if start_raw else end_ts - DURATIONS[duration]
    return UpDownMarket(
        slug=m["slug"],
        question=m.get("question", ""),
        condition_id=m.get("conditionId", ""),
        up_token=str(tokens[0]),
        down_token=str(tokens[1]),
        start_ts=start_ts,
        end_ts=end_ts,
        duration=duration,
        twap_seconds=int(cfg.get("twapLookbackSeconds", 60)) if cfg.get("twapEnabled", True) else 0,
        min_order_size=float(m.get("orderMinSize") or 5),
        tick_size=float(m.get("orderPriceMinTickSize") or 0.01),
        fee=FeeModel(
            rate=float(fs.get("rate", 0.07)),
            exponent=float(fs.get("exponent", 1.0)),
            enabled=bool(m.get("feesEnabled", True)),
        ),
        accepting_orders=bool(m.get("acceptingOrders")),
        closed=bool(m.get("closed")),
        outcome_prices=(float(prices[0]), float(prices[1])) if prices else None,
        raw=m,
    )


@dataclass(frozen=True)
class Level:
    price: float
    size: float


@dataclass
class Book:
    token_id: str
    bids: list[Level]  # best (highest) first
    asks: list[Level]  # best (lowest) first
    ts: float

    @property
    def best_bid(self) -> float | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0].price if self.asks else None

    def ask_depth(self, max_price: float) -> float:
        """Shares available at or below ``max_price``."""
        return sum(l.size for l in self.asks if l.price <= max_price + 1e-12)


def parse_book(d: dict) -> Book:
    # The CLOB returns bids ascending and asks descending (best level LAST),
    # so sort explicitly rather than trusting the order.
    bids = sorted((Level(float(x["price"]), float(x["size"])) for x in d.get("bids", [])),
                  key=lambda l: -l.price)
    asks = sorted((Level(float(x["price"]), float(x["size"])) for x in d.get("asks", [])),
                  key=lambda l: l.price)
    return Book(str(d.get("asset_id", "")), bids, asks, int(d.get("timestamp", 0)) / 1000.0)


class PolymarketClient:
    def __init__(
        self,
        transport: Transport,
        gamma_base: str = "https://gamma-api.polymarket.com",
        clob_base: str = "https://clob.polymarket.com",
    ):
        self.t = transport
        self.gamma = gamma_base.rstrip("/")
        self.clob = clob_base.rstrip("/")

    def updown_market(self, asset: str, duration: str, start_ts: int) -> UpDownMarket | None:
        slug = f"{asset}-updown-{duration}-{start_ts}"
        events = self.t.get_json(f"{self.gamma}/events?slug={slug}")
        if not events:
            return None
        markets = events[0].get("markets") or []
        return parse_market(markets[0], duration) if markets else None

    def live_updown_markets(self, asset: str, durations: list[str], now: float) -> list[UpDownMarket]:
        out = []
        for d in durations:
            step = DURATIONS[d]
            start = int(now) - int(now) % step
            try:
                m = self.updown_market(asset, d, start)
            except (HttpError, ValueError, KeyError):
                continue
            if m and m.accepting_orders and not m.closed:
                out.append(m)
        return out

    def book(self, token_id: str) -> Book:
        return parse_book(self.t.get_json(f"{self.clob}/book?token_id={token_id}"))

    def geoblock(self) -> dict:
        """{"blocked": bool, "country": ..} — Polymarket's own eligibility check."""
        return self.t.get_json("https://polymarket.com/api/geoblock")
