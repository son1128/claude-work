"""CLI: python -m grokdesk {scan|paper|live|calibrate|stats}"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
import time

from . import calibrate
from .config import Config, load
from .desk import Desk
from .execution import LiveExecutor, PaperExecutor, assert_live_allowed
from .feeds.polymarket import PolymarketClient
from .feeds.spot import SpotFeed
from .grok import GrokSupervisor
from .http import UrllibTransport
from .ledger import Ledger


def build(cfg: Config):
    t = UrllibTransport(timeout=cfg.feeds.http_timeout_s)
    spot = SpotFeed(t, cfg.feeds.binance_base, cfg.feeds.symbol, cfg.feeds.coinbase_product)
    poly = PolymarketClient(t, cfg.feeds.gamma_base, cfg.feeds.clob_base)
    return t, spot, poly


def cmd_scan(cfg: Config, _: argparse.Namespace) -> int:
    """One read-only pass: list live windows with model vs market. No orders."""
    t, spot, poly = build(cfg)
    spot.warm_up(max(cfg.prior.vol_lookback_s, cfg.prior.trend_lookback_s) + 5)
    spot.poll()
    led = Ledger(f"{cfg.desk.data_dir}/scan", cfg.desk.starting_bankroll_usd)
    desk = Desk(cfg, spot, poly, PaperExecutor(), GrokSupervisor(cfg.grok, t), led)
    now = time.time()
    desk.discover(now)
    if not desk.markets:
        print("no live windows found for", cfg.desk.asset, cfg.desk.durations)
    for m in desk.markets:
        k = desk.open_price(m)
        sig = desk.prior.price(m, k, list(spot.ticks), now)
        up, dn = poly.book(m.up_token), poly.book(m.down_token)
        print(f"\n{m.question}  ({m.slug})  {m.end_ts - now:.0f}s left")
        print(f"  open={k:,.2f}  spot={spot.latest().price:,.2f} [{spot.last_source}]  "
              f"fee rate={m.fee.rate} exp={m.fee.exponent}")
        print(f"  UP   bid/ask {up.best_bid}/{up.best_ask}   DOWN bid/ask {dn.best_bid}/{dn.best_ask}")
        if sig:
            _, notes = desk.edge.evaluate(m, sig, up, dn)
            print(f"  model P(UP)={sig.p_up:.3f} (no-drift {sig.p_up_no_drift:.3f})  "
                  f"vol={sig.vol_per_sec * 1e4 * 60 ** 0.5:.2f}bp/√min  R²={sig.r2:.2f}")
            for n in notes:
                print("   ", json.dumps(n))
    try:
        geo = poly.geoblock()
        print(f"\nPolymarket geoblock: blocked={geo.get('blocked')} country={geo.get('country')}")
    except Exception as e:  # noqa: BLE001 - informational only
        print("\ngeoblock check failed:", e)
    return 0


def cmd_run(cfg: Config, args: argparse.Namespace, live: bool) -> int:
    t, spot, poly = build(cfg)
    if live:
        try:
            assert_live_allowed(poly.geoblock(), cfg.desk.mode)
        except PermissionError as e:
            print("LIVE REFUSED:", e, file=sys.stderr)
            return 2
        executor = LiveExecutor(cfg.feeds.clob_base)
        data_dir = f"{cfg.desk.data_dir}/live"
    else:
        executor = PaperExecutor()
        data_dir = f"{cfg.desk.data_dir}/paper"
    led = Ledger(data_dir, cfg.desk.starting_bankroll_usd)
    n = spot.warm_up(max(cfg.prior.vol_lookback_s, cfg.prior.trend_lookback_s) + 5)
    print(f"[{executor.mode}] warmed up with {n} ticks; bankroll ${led.bankroll():.2f}; "
          f"grok={'on' if cfg.grok.enabled else 'off'} ({cfg.grok.model}); trace -> {data_dir}/trace.jsonl")
    desk = Desk(cfg, spot, poly, executor, GrokSupervisor(cfg.grok, t), led)
    try:
        desk.run(args.steps)
    except KeyboardInterrupt:
        pass
    print("final", json.dumps(led.stats()))
    return 0


def cmd_calibrate(cfg: Config, args: argparse.Namespace) -> int:
    _, spot, _ = build(cfg)
    prior = dataclasses.replace(cfg.prior, drift_weight=args.drift_weight)
    res = calibrate.run(spot, int(time.time()), args.windows, args.duration, prior_cfg=prior)
    print(f"calibration over last {args.windows} {args.duration} windows (Binance 1s replay), "
          f"drift arm uses drift_weight={args.drift_weight}")
    for c, row in sorted(res.items(), reverse=True):
        print(f"  T-{c:>4}s  {json.dumps(row)}")
    print("lower Brier is better; 0.25 = coin flip. Compare 'brier' vs 'brier_no_drift'.")
    return 0


def cmd_stats(cfg: Config, args: argparse.Namespace) -> int:
    led = Ledger(f"{cfg.desk.data_dir}/{args.book}", cfg.desk.starting_bankroll_usd)
    print(json.dumps(led.stats(), indent=1))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="grokdesk", description=__doc__)
    ap.add_argument("-c", "--config", default=None, help="path to config.toml")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("scan", help="read-only snapshot of live windows")
    p = sub.add_parser("paper", help="run the desk with simulated fills (default, safe)")
    p.add_argument("--steps", type=int, default=None)
    p = sub.add_parser("live", help="REAL MONEY. Requires mode=live, env confirm, unrestricted region")
    p.add_argument("--steps", type=int, default=None)
    p = sub.add_parser("calibrate", help="score the pricing model on past windows")
    p.add_argument("--windows", type=int, default=48)
    p.add_argument("--duration", default="15m", choices=["5m", "15m", "1h"])
    p.add_argument("--drift-weight", type=float, default=0.5,
                   help="drift_weight for the 'brier' arm ('brier_no_drift' always uses 0)")
    p = sub.add_parser("stats", help="print ledger stats")
    p.add_argument("--book", default="paper", choices=["paper", "live"])
    args = ap.parse_args(argv)
    cfg = load(args.config)
    if args.cmd == "scan":
        return cmd_scan(cfg, args)
    if args.cmd == "paper":
        return cmd_run(cfg, args, live=False)
    if args.cmd == "live":
        return cmd_run(cfg, args, live=True)
    if args.cmd == "calibrate":
        return cmd_calibrate(cfg, args)
    return cmd_stats(cfg, args)


if __name__ == "__main__":
    sys.exit(main())
