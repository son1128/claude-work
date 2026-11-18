"""Inline engine.js and the replay data into page.html -> one self-contained page.

    python web/build.py      # data/replay.json -> data/grok-desk.html

The result is what gets published as the Grok Desk artifact (no external
requests besides Google Fonts; the replay data is embedded).
"""

from __future__ import annotations

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def build(data_path: str, out_path: str) -> int:
    with open(os.path.join(HERE, "page.html"), encoding="utf-8") as f:
        page = f.read()
    with open(os.path.join(HERE, "engine.js"), encoding="utf-8") as f:
        engine = f.read().replace('if (typeof module !== "undefined") module.exports = GD;', "")
    with open(data_path, encoding="utf-8") as f:
        data = f.read()
    if "</script" in data.lower():
        raise ValueError("replay data must not contain </script")
    for marker in ("/*__DATA__*/", "/*__ENGINE__*/"):
        if page.count(marker) != 1:
            raise ValueError(f"page.html must contain {marker} exactly once")
    html = page.replace("/*__DATA__*/", "window.__REPLAY__=" + data + ";").replace("/*__ENGINE__*/", engine)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    return len(html)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=os.path.join(ROOT, "data", "replay.json"))
    ap.add_argument("--out", default=os.path.join(ROOT, "data", "grok-desk.html"))
    a = ap.parse_args()
    n = build(a.data, a.out)
    print(f"wrote {a.out} ({n / 1024:.0f} KiB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
