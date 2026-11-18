"""The desk: one loop that runs SPOTTER -> PRIOR -> EDGE -> KELLY -> RISK -> GROK -> TAKER,
and CLOSER for expired positions. ``step()`` is a single, deterministic tick so the
whole pipeline can be driven by tests with fake feeds and a fake clock."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

from .agents import EdgeAgent, KellyAgent, PriorAgent
from .config import Config
from .execution import Executor
from .feeds.polymarket import Book, PolymarketClient, UpDownMarket
from .feeds.spot import SpotFeed
from .grok import GrokSupervisor
from .http import HttpError
from .ledger import Ledger, Position
from .risk import RiskManager, RiskState


@dataclass
class Desk:
    cfg: Config
    spot: SpotFeed
    poly: PolymarketClient
    executor: Executor
    grok: GrokSupervisor
    ledger: Ledger
    clock: Callable[[], float] = time.time
    log: Callable[[str], None] = print
    markets: list[UpDownMarket] = field(default_factory=list)
    _open_px: dict[str, float] = field(default_factory=dict)
    _books: dict[str, tuple[float, Book]] = field(default_factory=dict)
    _last_discover: float = 0.0

    def __post_init__(self) -> None:
        self.prior = PriorAgent(self.cfg.prior)
        self.edge = EdgeAgent(self.cfg.edge)
        self.kelly = KellyAgent(self.cfg.kelly)
        self.risk = RiskManager(self.cfg.risk)

    # -- SPOTTER -----------------------------------------------------------
    def discover(self, now: float) -> None:
        if now - self._last_discover < self.cfg.desk.discover_interval_s and self.markets:
            return
        self._last_discover = now
        self.markets = self.poly.live_updown_markets(self.cfg.desk.asset, self.cfg.desk.durations, now)
        live = {m.slug for m in self.markets}
        self._open_px = {k: v for k, v in self._open_px.items() if k in live or self._has_open(k)}

    def _has_open(self, slug: str) -> bool:
        return any(p.slug == slug for p in self.ledger.open_positions())

    def open_price(self, m: UpDownMarket) -> float:
        if m.slug not in self._open_px:
            start = int(m.start_ts)
            if self.cfg.desk.open_price_mode == "twap" and m.twap_seconds:
                self._open_px[m.slug] = self.spot.twap(start, m.twap_seconds)
            else:
                self._open_px[m.slug] = self.spot.price_at(start)
        return self._open_px[m.slug]

    def _book(self, token: str, now: float) -> Book:
        hit = self._books.get(token)
        if hit and now - hit[0] < self.cfg.desk.book_refresh_s:
            return hit[1]
        b = self.poly.book(token)
        self._books[token] = (now, b)
        return b

    # -- one tick ----------------------------------------------------------
    def step(self) -> None:
        now = self.clock()
        try:
            self.spot.poll()
        except HttpError as e:
            self.ledger.trace("feed_error", error=str(e))
        try:
            self.discover(now)
        except HttpError as e:
            self.ledger.trace("discover_error", error=str(e))
        for m in self.markets:
            if m.start_ts <= now < m.end_ts:
                try:
                    self.consider(m, now)
                except HttpError as e:
                    self.ledger.trace("market_error", slug=m.slug, error=str(e))
        self.settle_due(now)

    def consider(self, m: UpDownMarket, now: float) -> None:
        tr = self.ledger.trace
        seconds_left = m.end_ts - now
        last = self.ledger.last_trade_ts(m.slug)
        day_start = now - now % 86400
        st = RiskState(
            realized_pnl_today=self.ledger.realized_pnl_since(day_start),
            open_positions=len(self.ledger.open_positions()),
            open_in_market=sum(1 for p in self.ledger.open_positions() if p.slug == m.slug),
            seconds_since_last_trade_in_market=(now - last) if last else None,
        )
        blocked = self.risk.check(seconds_left, st)
        if blocked:
            return  # cheap pre-check; not traced every second

        k = self.open_price(m)
        sig = self.prior.price(m, k, list(self.spot.ticks), now)
        if sig is None:
            tr("prior_skip", slug=m.slug, reason="not enough fresh ticks")
            return
        up_b, dn_b = self._book(m.up_token, now), self._book(m.down_token, now)
        opp, notes = self.edge.evaluate(m, sig, up_b, dn_b)
        if opp is None:
            tr("no_edge", slug=m.slug, signal=sig.to_dict(), sides=notes)
            return
        book = up_b if opp.side == "UP" else dn_b
        size = self.kelly.size(opp, self.ledger.bankroll(), m.min_order_size)
        if size.shares <= 0:
            tr("size_skip", slug=m.slug, opp=opp.to_dict(), reason=size.reason)
            return

        verdict = self.grok.review({
            "market": m.question,
            "seconds_left": round(seconds_left, 1),
            "signal": sig.to_dict(),
            "proposal": {**opp.to_dict(), "shares": size.shares, "stake_usd": size.stake_usd},
            "book": {"best_bid": book.best_bid, "best_ask": book.best_ask,
                     "spread": None if book.best_bid is None or book.best_ask is None
                     else round(book.best_ask - book.best_bid, 4)},
            "feed": {"source": self.spot.last_source, "stale_s": round(sig.stale_seconds, 2)},
        })
        tr("grok", slug=m.slug, approve=verdict.approve, reason=verdict.reason,
           source=verdict.source, cost_usd=round(verdict.cost_usd, 6), spent_today=round(self.grok.spent_usd, 4))
        if not verdict.approve:
            return

        fill = self.executor.buy(m, opp, size, book)
        if fill is None:
            tr("fill_fail", slug=m.slug, opp=opp.to_dict(), shares=size.shares)
            return
        pos = Position(
            id=fill.order_id, mode=self.executor.mode, slug=m.slug, side=opp.side,
            token_id=opp.token_id, shares=fill.shares, avg_price=fill.avg_price,
            fee_usd=fill.fee_usd, opened_ts=now, end_ts=m.end_ts, open_price=k, p_win=opp.p_win,
        )
        self.ledger.add(pos)
        tr("fill", slug=m.slug, side=opp.side, shares=fill.shares, avg=round(fill.avg_price, 4),
           fee=fill.fee_usd, p_win=round(opp.p_win, 4), edge=round(opp.edge_per_share, 4))
        self.log(f"[{self.executor.mode}] BUY {opp.side} {fill.shares:g} @ {fill.avg_price:.3f} "
                 f"{m.slug}  p={opp.p_win:.3f} edge={opp.edge_per_share:+.3f}  {verdict.source}: {verdict.reason}")

    # -- CLOSER ------------------------------------------------------------
    def settle_due(self, now: float) -> None:
        d = self.cfg.desk
        for p in self.ledger.open_positions():
            if now < p.end_ts + d.settle_grace_s:
                continue
            up_won, by = None, ""
            try:
                dur, start = p.slug.split("-")[-2], int(p.slug.split("-")[-1])
                m = self.poly.updown_market(self.cfg.desk.asset, dur, start)
                if m is not None and m.resolved_up is not None:
                    up_won, by = m.resolved_up, "gamma"
            except (HttpError, ValueError, KeyError):
                pass
            if up_won is None and now >= p.end_ts + d.proxy_settle_after_s:
                try:
                    up_won = self.spot.twap(int(p.end_ts), 60) >= p.open_price
                    by = "spot_proxy"
                except HttpError:
                    continue
            if up_won is None:
                continue
            self.ledger.settle(p, up_won, by)
            self.ledger.trace("settle", slug=p.slug, side=p.side, status=p.status,
                              pnl=p.pnl_usd, by=by)
            self.log(f"[{p.mode}] SETTLE {p.slug} {p.side} -> {p.status} pnl={p.pnl_usd:+.2f} ({by})")

    def run(self, max_steps: int | None = None) -> None:
        n = 0
        while max_steps is None or n < max_steps:
            t0 = self.clock()
            self.step()
            n += 1
            if n % 60 == 0:
                self.log(f"stats {self.ledger.stats()}  grok_spent=${self.grok.spent_usd:.4f}")
            time.sleep(max(0.0, self.cfg.desk.poll_interval_s - (self.clock() - t0)))
