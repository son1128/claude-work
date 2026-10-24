"""사례 10건을 실제 시세로 다시 그립니다 (case_audit.py 결과와 캐시 사용).

사용법: python case_charts.py CACHE_DIR AUDIT_JSON OUT_DIR
그림: 캔들 + EMA 60/120/240, 진입(▲▼)·청산(●), 문서의 손절 기준선(빨강 점선), 사용 레버리지의 청산가(주황 점선),
      아래 창에 PPP 재현 오실레이터와 크로스.
"""
import json
import os
import sys
from datetime import datetime

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

import case_audit as ca  # noqa: E402
import ppp_core as pc  # noqa: E402

BARS = {"1m": 150, "5m": 140, "15m": 120, "1h": 96}


def draw(c, r, cache, out):
    a_ms, base, d1, h4 = ca.load_case(c, cache)
    tf = c["tf"]
    f = ca.resample(base, tf) if c["src"] == "bingx" or tf != "15m" else base.copy()
    t = f["time"].to_numpy()
    t_in = int(datetime.strptime("2026-" + r["entry_time_kst"], "%Y-%m-%d %H:%M").replace(tzinfo=ca.KST).timestamp() * 1000)
    t_out = int(datetime.strptime("2026-" + r["exit_time_kst"], "%Y-%m-%d %H:%M").replace(tzinfo=ca.KST).timestamp() * 1000)
    i_in = int(np.searchsorted(t, t_in, side="right") - 1)
    i_out = int(np.searchsorted(t, t_out, side="right") - 1)
    n = BARS[tf]
    pad = max(10, n // 6)
    hi_i = min(len(f), i_out + pad)
    lo_i = max(0, min(hi_i - n, i_in - pad))     # 보유가 길면 진입 시점까지 넓힘
    cl = f["close"].to_numpy()
    osc = pc.ppp_oscillator(cl)
    emas = {k: pc.pine_ema(cl, k) for k in (60, 120, 240)}
    sl = slice(lo_i, hi_i)
    x = np.arange(hi_i - lo_i)
    o, h, lw, cc = (f[k].to_numpy()[sl] for k in ("open", "high", "low", "close"))
    fig, (ax, ax2) = plt.subplots(2, 1, figsize=(8.4, 4.6), dpi=110, gridspec_kw={"height_ratios": [2.6, 1]}, sharex=True)
    fig.patch.set_facecolor("#000000")
    for a_ in (ax, ax2):
        a_.set_facecolor("#000000")
        a_.tick_params(colors="#9e9e9e", labelsize=7)
        for s in a_.spines.values():
            s.set_color("#333333")
    up = cc >= o
    ax.vlines(x, lw, h, color=np.where(up, "#26a69a", "#ef5350"), linewidth=0.8)
    ax.bar(x, np.abs(cc - o) + 1e-12, bottom=np.minimum(o, cc), color=np.where(up, "#26a69a", "#ef5350"), width=0.7)
    for k, col, w in ((60, "#ff4f9a", 1.0), (120, "#b388ff", 1.0), (240, "#ffffff", 1.4)):
        ax.plot(x, emas[k][sl], color=col, lw=w, label=f"EMA{k}")
    side = c["side"]
    xi, xo = i_in - lo_i, i_out - lo_i
    ax.scatter([xi], [c["entry"]], marker="^" if side > 0 else "v", s=70, color="#ffd400", zorder=6, label="entry")
    ax.scatter([xo], [c["exit"]], marker="o", s=45, color="#69f0ae", zorder=6, label="exit")
    liq = c["entry"] * (1 - side * (1 / c["lev"] - 0.01))
    ax.axhline(liq, color="#ff9100", ls="--", lw=1, label=f"liq {c['lev']}x")
    if r.get("stop_level") and c["ma"][0] == tf:
        ax.axhline(r["stop_level"], color="#ff5252", ls=":", lw=1.2, label=f"stop ref {r['stop_ma']}")
    elif r.get("stop_level"):
        ymin, ymax = min(lw.min(), liq), max(h.max(), liq)
        if ymin <= r["stop_level"] <= ymax:
            ax.axhline(r["stop_level"], color="#ff5252", ls=":", lw=1.2, label=f"stop ref {r['stop_ma']}")
        else:
            ax.text(0.01, 0.04 if side > 0 else 0.92, f"stop ref {r['stop_ma']} = {r['stop_level']:.5g} (off chart, {r['stop_dist_pct']:.1f}% away)",
                    transform=ax.transAxes, color="#ff5252", fontsize=7)
    ax.legend(loc="upper left", fontsize=6, facecolor="#111111", edgecolor="#333333", labelcolor="#dddddd", ncol=3)
    ax.set_title(f"#{c['no']} {c['sym']}USDT  {tf}  {'LONG' if side > 0 else 'SHORT'} {c['lev']}x  (BingX/Gate real data, KST)",
                 color="#dddddd", fontsize=9, loc="left")
    k, d = osc["k"][sl], osc["d"][sl]
    ax2.plot(x, d, color="#ff4f9a", lw=1.3)
    ax2.plot(x, k, color="#ffffff", lw=1.0)
    ax2.fill_between(x, k, d, where=k >= d, color="#8e3b3b", alpha=0.6, lw=0)
    ax2.fill_between(x, k, d, where=k < d, color="#4f5530", alpha=0.6, lw=0)
    for y in (25, 75):
        ax2.axhline(y, color="#666666", lw=0.6, ls="--")
    gc, dc, lvl = osc["gc"][sl], osc["dc"][sl], osc["lvl"][sl]
    ax2.scatter(x[gc], lvl[gc], s=18, facecolors="none", edgecolors="#ffd400", lw=1)
    ax2.scatter(x[dc], lvl[dc], s=18, facecolors="none", edgecolors="#2ecc71", lw=1)
    ax2.axvline(xi, color="#ffd400", lw=0.8, ls=":")
    ax2.set_ylim(-5, 105)
    lab = [datetime.fromtimestamp(v / 1000, ca.KST).strftime("%m-%d %H:%M") for v in t[sl]]
    ticks = np.linspace(0, len(x) - 1, 6).astype(int)
    ax2.set_xticks(ticks)
    ax2.set_xticklabels([lab[i] for i in ticks])
    ax.yaxis.tick_right()
    ax2.yaxis.tick_right()
    fig.tight_layout()
    p = os.path.join(out, f"case-{c['no']:02d}-{c['sym'].lower()}.png")
    fig.savefig(p, facecolor=fig.get_facecolor())
    plt.close(fig)
    return p


def main(cache, audit, out):
    os.makedirs(out, exist_ok=True)
    res = {x["no"]: x for x in json.load(open(audit))}
    for c in ca.CASES:
        r = res.get(c["no"])
        if r and r.get("found"):
            print(draw(c, r, cache, out), flush=True)


if __name__ == "__main__":
    main(*sys.argv[1:4])
