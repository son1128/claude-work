"""Spot BTC price feed used as a proxy for the Chainlink BTC/USD stream.

Default source is Binance's public market-data mirror (data-api.binance.vision),
which serves the same /api/v3 endpoints as api.binance.com without a key.
Coinbase is the fallback. A rolling buffer keeps ~1 sample/second.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Deque

from ..http import HttpError, Transport


@dataclass(frozen=True)
class Tick:
    ts: float  # unix seconds
    price: float


class SpotFeed:
    def __init__(
        self,
        transport: Transport,
        binance_base: str = "https://data-api.binance.vision",
        symbol: str = "BTCUSDT",
        coinbase_product: str = "BTC-USD",
        buffer_seconds: int = 1800,
        clock: Callable[[], float] = time.time,
    ):
        self.t = transport
        self.binance_base = binance_base.rstrip("/")
        self.symbol = symbol
        self.coinbase_product = coinbase_product
        self.clock = clock
        self.ticks: Deque[Tick] = deque(maxlen=buffer_seconds)
        self.last_source = ""

    # -- live sampling -----------------------------------------------------
    def poll(self) -> Tick:
        now = self.clock()
        try:
            d = self.t.get_json(
                f"{self.binance_base}/api/v3/ticker/price?symbol={self.symbol}"
            )
            price, self.last_source = float(d["price"]), "binance"
        except (HttpError, KeyError, ValueError):
            d = self.t.get_json(
                f"https://api.exchange.coinbase.com/products/{self.coinbase_product}/ticker"
            )
            price, self.last_source = float(d["price"]), "coinbase"
        tick = Tick(now, price)
        self.add(tick)
        return tick

    def add(self, tick: Tick) -> None:
        if self.ticks and tick.ts <= self.ticks[-1].ts:
            return
        self.ticks.append(tick)

    def warm_up(self, seconds: int = 600) -> int:
        """Backfill the buffer from 1-second klines so the model can start now."""
        rows = self.klines_1s(int(self.clock()) - seconds, seconds)
        for ts, close in rows:
            self.add(Tick(ts, close))
        return len(rows)

    def window(self, seconds: float) -> list[Tick]:
        cutoff = self.clock() - seconds
        return [t for t in self.ticks if t.ts >= cutoff]

    def latest(self) -> Tick | None:
        return self.ticks[-1] if self.ticks else None

    # -- history -----------------------------------------------------------
    def klines_1s(self, start_ts: int, count: int) -> list[tuple[float, float]]:
        """[(close_time_seconds, close)] for 1s candles starting at start_ts."""
        out: list[tuple[float, float]] = []
        cursor = start_ts
        while count > 0:
            n = min(1000, count)
            rows = self.t.get_json(
                f"{self.binance_base}/api/v3/klines?symbol={self.symbol}"
                f"&interval=1s&startTime={cursor * 1000}&limit={n}"
            )
            if not rows:
                break
            for r in rows:
                out.append((int(r[0]) / 1000.0 + 1.0, float(r[4])))
            cursor = int(rows[-1][0]) // 1000 + 1
            count -= len(rows)
            if len(rows) < n:
                break
        return out

    def price_at(self, ts: int) -> float:
        """Open of the 1s candle at ``ts`` (proxy for 'price at window start')."""
        rows = self.t.get_json(
            f"{self.binance_base}/api/v3/klines?symbol={self.symbol}"
            f"&interval=1s&startTime={ts * 1000}&limit=1"
        )
        if not rows:
            raise HttpError("klines", None, f"no candle at {ts}")
        return float(rows[0][1])

    def twap(self, end_ts: int, seconds: int) -> float:
        rows = self.klines_1s(end_ts - seconds, seconds)
        if not rows:
            raise HttpError("klines", None, f"no candles before {end_ts}")
        return sum(p for _, p in rows) / len(rows)
