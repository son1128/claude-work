"""Load config.toml into typed dataclasses (stdlib tomllib, Python 3.11+)."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields
from typing import Any

from .agents import EdgeConfig, KellyConfig, PriorConfig
from .grok import GrokConfig
from .risk import RiskConfig


@dataclass
class DeskConfig:
    mode: str = "paper"  # paper | live
    asset: str = "btc"
    durations: list[str] = field(default_factory=lambda: ["15m"])
    starting_bankroll_usd: float = 500.0
    data_dir: str = "data"
    poll_interval_s: float = 1.0
    discover_interval_s: float = 15.0
    book_refresh_s: float = 2.0
    open_price_mode: str = "spot"  # "spot": 1s candle at window start; "twap": 60s TWAP ending at start
    settle_grace_s: float = 90.0  # wait this long after expiry before asking Gamma
    proxy_settle_after_s: float = 900.0  # then fall back to our own spot-TWAP estimate


@dataclass
class FeedConfig:
    binance_base: str = "https://data-api.binance.vision"
    symbol: str = "BTCUSDT"
    coinbase_product: str = "BTC-USD"
    gamma_base: str = "https://gamma-api.polymarket.com"
    clob_base: str = "https://clob.polymarket.com"
    http_timeout_s: float = 8.0


@dataclass
class Config:
    desk: DeskConfig = field(default_factory=DeskConfig)
    feeds: FeedConfig = field(default_factory=FeedConfig)
    prior: PriorConfig = field(default_factory=PriorConfig)
    edge: EdgeConfig = field(default_factory=EdgeConfig)
    kelly: KellyConfig = field(default_factory=KellyConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    grok: GrokConfig = field(default_factory=GrokConfig)


def _fill(cls: type, data: dict[str, Any], section: str) -> Any:
    known = {f.name for f in fields(cls)}
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"unknown keys in [{section}]: {', '.join(sorted(unknown))}")
    return cls(**data)


def from_dict(d: dict[str, Any]) -> Config:
    unknown = set(d) - {f.name for f in fields(Config)}
    if unknown:
        raise ValueError(f"unknown config sections: {', '.join(sorted(unknown))}")
    kw = {}
    for f in fields(Config):
        kw[f.name] = _fill(f.default_factory().__class__, d.get(f.name, {}), f.name)  # type: ignore[misc]
    cfg = Config(**kw)
    if cfg.desk.mode not in ("paper", "live"):
        raise ValueError("[desk].mode must be 'paper' or 'live'")
    if cfg.grok.fail_mode not in ("veto", "approve"):
        raise ValueError("[grok].fail_mode must be 'veto' or 'approve'")
    return cfg


def load(path: str | None) -> Config:
    if not path:
        return Config()
    with open(path, "rb") as f:
        return from_dict(tomllib.load(f))
