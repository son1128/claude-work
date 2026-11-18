"""Download recent closed BTC 15m Up/Down windows for the web replay.

For each window: Binance 1-second closes (300 s lookback + the 900 s window,
integer cents, delta-encoded), Polymarket's ~1-minute Up-token price history,
the market's fee schedule, and the actual resolution from Gamma.

    python web/fetch_replay.py --windows 96      # writes data/replay.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from grokdesk.http import HttpError, UrllibTransport  # noqa: E402

BINANCE = "https://data-api.binance.vision/api/v3/klines?symbol=BTCUSDT&interval=1s"
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
LOOKBACK, STEP = 300, 900


def get(t: UrllibTransport, url: str, tries: int = 3):
    for i in range(tries):
        try:
            return t.get_json(url)
        except HttpError:
            if i == tries - 1:
                raise
            time.sleep(1 + i)


def window(t: UrllibTransport, start: int) -> dict | None:
    end = start + STEP
    ev = get(t, f"{GAMMA}/events?slug=btc-updown-15m-{start}")
    if not ev:
        return None
    m = ev[0]["markets"][0]
    outcome = json.loads(m["outcomePrices"])
    tokens = json.loads(m["clobTokenIds"])
    if not m.get("closed") or outcome[0] not in ("0", "1"):
        return None  # not resolved yet

    rows, cur = [], start - LOOKBACK
    while cur < end:
        r = get(t, f"{BINANCE}&startTime={cur * 1000}&limit=1000")
        if not r:
            break
        rows += r
        cur = int(r[-1][0]) // 1000 + 1

    t0 = start - LOOKBACK
    px: list[int | None] = [None] * (LOOKBACK + STEP)
    for r in rows:
        i = int(r[0]) // 1000 - t0
        if 0 <= i < len(px):
            px[i] = round(float(r[4]) * 100)
    last = None
    for i, v in enumerate(px):  # carry forward over missing seconds
        px[i] = v if v is not None else last
        last = px[i]
    if px[0] is None:
        return None

    hist = get(t, f"{CLOB}/prices-history?market={tokens[0]}&startTs={start}&endTs={end}&fidelity=1")
    fs = m.get("feeSchedule") or {}
    return {
        "start": start,
        "up": outcome[0] == "1",
        "q": m["question"],
        "fee": [fs.get("rate", 0.07), fs.get("exponent", 1)],
        "spot": [px[0]] + [px[i] - px[i - 1] for i in range(1, len(px))],
        "mkt": [[p["t"] - start, round(p["p"], 4)] for p in hist.get("history", []) if start <= p["t"] < end],
    }


def main() -> int:
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ap = argparse.ArgumentParser()
    ap.add_argument("--windows", type=int, default=96)
    ap.add_argument("--out", default=os.path.join(here, "data", "replay.json"))
    args = ap.parse_args()

    t = UrllibTransport(timeout=25)
    now = int(time.time())
    last = now - now % STEP - STEP
    starts = [last - i * STEP for i in range(args.windows)]
    with ThreadPoolExecutor(6) as ex:
        res = [w for w in ex.map(lambda s: window(t, s), starts) if w]
    res.sort(key=lambda w: w["start"])
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump({"generated": now, "lookback": LOOKBACK, "step": STEP, "windows": res}, f, separators=(",", ":"))
    print(f"{len(res)} windows, {sum(len(w['mkt']) for w in res)} market points -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
