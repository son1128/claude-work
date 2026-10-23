"""PPP 재현 지표 검증 실행 스크립트.

사용법:
    python fetch_klines.py DATA_DIR 75 BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,DOGEUSDT
    python run_verify.py DATA_DIR OUT_DIR

DATA_DIR/<SYMBOL>_1m.csv 를 읽어 다음을 계산하고 OUT_DIR에 results.json과 그림을 저장합니다.
  1) 구현 자체 점검 (값 범위, 크로스 정의, 띠 색 4상태)
  2) 그림 모양 비교용 통계 (파동 고점·저점 분포, 15분봉 화면 재현)
  3) 문서 주장 검증: 과매수=롱 힘 최강, 막 골크 vs 늦은 골크
  4) 백테스트: 크로스 단독 / 구간 크로스 / 4P 전체 규칙
"""
import json
import os
import sys

import numpy as np
import pandas as pd

import ppp_core as pc

FEE = 0.0005


def load(path):
    df = pd.read_csv(path)
    return df.sort_values("time").reset_index(drop=True)


def resample(df, rule):
    t = pd.to_datetime(df["time"], unit="ms")
    g = df.set_index(t).resample(rule, label="left", closed="left")
    out = pd.DataFrame({"open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(),
                        "close": g["close"].last(), "volume": g["volume"].sum()}).dropna()
    out["time"] = (out.index - pd.Timestamp(0)) // pd.Timedelta(milliseconds=1)
    return out.reset_index(drop=True)


def self_checks(osc):
    k, d, st = osc["k"], osc["d"], osc["stoch"]
    v = ~np.isnan(k)
    gc, dc = osc["gc"].copy(), osc["dc"].copy()
    gc[0] = dc[0] = False                       # 구간을 잘라낸 첫 봉은 직전 값이 없음
    k1, d1 = np.roll(k, 1), np.roll(d, 1)
    return {
        "stoch_in_0_100": bool(np.nanmin(st) >= -1e-9 and np.nanmax(st) <= 100 + 1e-9),
        "k_in_0_100": bool(np.nanmin(k[v]) >= -1e-9 and np.nanmax(k[v]) <= 100 + 1e-9),
        "gc_definition": bool(np.all((k[gc] > d[gc]) & (k1[gc] <= d1[gc]))),
        "dc_definition": bool(np.all((k[dc] < d[dc]) & (k1[dc] >= d1[dc]))),
        "no_gc_dc_same_bar": bool(not np.any(gc & dc)),
        "state_counts": {str(s): int((osc["state"] == s).sum()) for s in (1, 2, 3, 4)},
        "state_sign_ok": bool(np.all((osc["state"][v] <= 2) == ((k - d)[v] >= 0))),
    }


def swing_stats(osc, warm=300):
    """K의 고점·저점(교차 사이 최대·최소) 분포 — 문서 그림(고점 85~95, 저점 13~22)과 비교."""
    k = osc["k"][warm:]
    cr = np.flatnonzero((osc["gc"] | osc["dc"])[warm:])
    peaks, troughs = [], []
    for a, b in zip(cr[:-1], cr[1:]):
        seg = k[a:b + 1]
        if osc["gc"][warm + a]:
            peaks.append(seg.max())
        else:
            troughs.append(seg.min())
    return {"peak_median": float(np.median(peaks)), "peak_p25": float(np.percentile(peaks, 25)),
            "trough_median": float(np.median(troughs)), "trough_p75": float(np.percentile(troughs, 75)),
            "cross_per_100_bars": float(len(cr) / len(k) * 100)}


def fwd_returns(close, n):
    f = np.full(len(close), np.nan)
    f[:-n] = close[n:] / close[:-n] - 1
    return f


def zone_test(osc, close, n=15):
    """'과매수 = 롱 힘 최강' 주장: K 구간별 다음 n봉 수익률."""
    f = fwd_returns(close, n)
    k = osc["k"]
    out = {}
    for name, m in (("K>75", k > 75), ("25~75", (k >= 25) & (k <= 75)), ("K<25", k < 25)):
        x = f[m & ~np.isnan(f)]
        out[name] = {"n": int(len(x)), "mean_bp": float(x.mean() * 1e4), "up_ratio": float((x > 0).mean())}
    return out


def fresh_vs_late(osc, sig, close, hold=30):
    """'막 골크 vs 늦은 골크' 주장: 1P 상승 방향 골든크로스에서 0/3/6/10봉 늦게 들어갔을 때 hold봉 수익."""
    idx = np.flatnonzero(osc["gc"] & (sig["hdir"] == 1))
    idx_s = np.flatnonzero(osc["dc"] & (sig["hdir"] == -1))
    out = {}
    for lag in (0, 3, 6, 10):
        rs = []
        for i in idx:
            a, b = i + lag, i + lag + hold
            if b < len(close):
                rs.append(close[b] / close[a] - 1)
        for i in idx_s:
            a, b = i + lag, i + lag + hold
            if b < len(close):
                rs.append(-(close[b] / close[a] - 1))
        rs = np.array(rs)
        out[f"lag{lag}"] = {"n": int(len(rs)), "mean_bp": float(rs.mean() * 1e4), "win": float((rs > 0).mean())}
    return out


def plot_oscillator(df, osc, path, title, n=180):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    cols = {1: "#8e3b3b", 2: "#f0a3a0", 3: "#4f5530", 4: "#f3e4a1"}
    sl = slice(len(df) - n, len(df))
    k, d, st = osc["k"][sl], osc["d"][sl], osc["state"][sl]
    x = np.arange(n)
    fig, ax = plt.subplots(figsize=(10, 3.6), dpi=110)
    fig.patch.set_facecolor("#000000"); ax.set_facecolor("#000000")
    for i in range(n - 1):
        ax.fill_between(x[i:i + 2], k[i:i + 2], d[i:i + 2], color=cols[int(st[i + 1])], lw=0)
    ax.plot(x, d, color="#ff4f9a", lw=1.6)
    ax.plot(x, k, color="#ffffff", lw=1.2)
    for y, ls in ((75, "--"), (25, "--")):
        ax.axhline(y, color="#777777", lw=0.7, ls=ls)
    ax.axhline(50, color="#ff4f9a", lw=0.7, alpha=0.7)
    gc, dc, lvl = osc["gc"][sl], osc["dc"][sl], osc["lvl"][sl]
    ax.scatter(x[gc], lvl[gc], s=30, facecolors="none", edgecolors="#ffd400", lw=1.2, zorder=5)
    ax.scatter(x[dc], lvl[dc], s=30, facecolors="none", edgecolors="#2ecc71", lw=1.2, zorder=5)
    ax.set_ylim(-5, 105); ax.set_xlim(0, n - 1)
    ax.yaxis.tick_right(); ax.tick_params(colors="#aaaaaa", labelsize=7)
    t = pd.to_datetime(df["time"].iloc[sl], unit="ms").dt.strftime("%m-%d %H:%M").to_numpy()
    ticks = np.linspace(0, n - 1, 7).astype(int)
    ax.set_xticks(ticks); ax.set_xticklabels(t[ticks])
    for s in ax.spines.values():
        s.set_color("#333333")
    ax.set_title(title, color="#dddddd", fontsize=9, loc="left")
    fig.tight_layout(); fig.savefig(path, facecolor=fig.get_facecolor()); plt.close(fig)


def sensitivity(frames, warm):
    """설정값·청산 방식·수수료를 바꿔도 결론이 같은지 확인 (5종목 합산)."""
    out = []
    for params in ((14, 14, 3, 3), (14, 14, 5, 5), (21, 21, 5, 5)):
        for exit_name, use_opp in (("반대 크로스+BB+EMA", True), ("BB+EMA만", False)):
            lst = []
            days = 0
            for df in frames:
                osc = pc.ppp_oscillator(df["close"].to_numpy(), *params)
                sig = pc.four_p_signals(df, osc)
                cut = lambda dct: {k: (v[warm:] if isinstance(v, np.ndarray) else [x[warm:] for x in v]) for k, v in dct.items()}
                test = df.iloc[warm:].reset_index(drop=True)
                t = pc.backtest(test, cut(osc), cut(sig), 0.0, use_opp=use_opp)
                days += len(test) / 1440
                if len(t):
                    lst.append(t)
            t = pd.concat(lst)
            row = {"params": list(params), "exit": exit_name}
            for fee in (0.0, 0.0002, 0.0005):
                tt = t.assign(net=t["gross"] - 2 * fee)
                sm = pc.summarize(tt, days / len(frames))
                row[f"fee{fee}"] = {k: sm[k] for k in ("trades", "win_net", "pf", "avg_win", "avg_loss")}
            row["win_gross"] = float((t["gross"] > 0).mean())
            row["avg_bars"] = float(t["bars"].mean())
            row["per_day_per_symbol"] = len(t) / days
            out.append(row)
            print(row["params"], exit_name, round(row["win_gross"], 3), row["fee0.0"]["pf"], flush=True)
    return out


def main(data_dir, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    files = sorted(f for f in os.listdir(data_dir) if f.endswith("_1m.csv"))
    res = {"symbols": {}, "fee_per_side": FEE}
    all_trades = {"cross": [], "zone": [], "fourp": [], "fourp_zone": []}
    for f in files:
        sym = f.split("_")[0]
        df = load(os.path.join(data_dir, f))
        warm = 20 * 24 * 60                         # 1P(1시간 EMA240) 워밍업 20일 제외
        osc = pc.ppp_oscillator(df["close"].to_numpy())
        sig = pc.four_p_signals(df, osc)
        sig_z = pc.four_p_signals(df, osc, zone_only=True)
        test = df.iloc[warm:].reset_index(drop=True)
        cut = lambda dct: {k: (v[warm:] if isinstance(v, np.ndarray) else [x[warm:] for x in v]) for k, v in dct.items()}
        osc_t, sig_t, sigz_t = cut(osc), cut(sig), cut(sig_z)
        days = len(test) / 1440
        close = test["close"].to_numpy()
        r = {"bars": len(test), "days": days, "self_checks": self_checks(osc_t),
             "swing_1m": swing_stats(osc_t)}
        df15 = resample(df, "15min")
        osc15 = pc.ppp_oscillator(df15["close"].to_numpy())
        r["swing_15m"] = swing_stats(osc15, warm=100)
        r["zone_test_1m"] = zone_test(osc_t, close)
        r["fresh_vs_late_1m"] = fresh_vs_late(osc_t, sig_t, close)
        bt = {
            "cross": pc.simple_cross_backtest(osc_t, close, FEE),
            "zone": pc.simple_cross_backtest(osc_t, close, FEE, zone=True),
            "fourp": pc.backtest(test, osc_t, sig_t, FEE),
            "fourp_zone": pc.backtest(test, osc_t, sigz_t, FEE),
        }
        r["backtest"] = {k: pc.summarize(v, days) for k, v in bt.items()}
        r["fourp_exit_reasons"] = bt["fourp"]["reason"].value_counts().to_dict() if len(bt["fourp"]) else {}
        for k, v in bt.items():
            if len(v):
                all_trades[k].append(v.assign(sym=sym))
        res["symbols"][sym] = r
        if sym == "BTCUSDT":
            df60 = resample(df, "60min")
            plot_oscillator(df60, pc.ppp_oscillator(df60["close"].to_numpy()),
                            os.path.join(out_dir, "ppp-clone-1h.png"), "PPP clone - BTCUSDT 1h (14,14,3,3)", n=48)
            plot_oscillator(df, osc, os.path.join(out_dir, "ppp-clone-1m.png"), "PPP clone - BTCUSDT 1m (14,14,3,3)", n=240)
        print(sym, json.dumps(r["backtest"]["fourp"], ensure_ascii=False), flush=True)
    days_total = sum(v["days"] for v in res["symbols"].values())
    res["pooled"] = {}
    for k, lst in all_trades.items():
        t = pd.concat(lst).sort_values("i_out") if lst else pd.DataFrame()
        res["pooled"][k] = pc.summarize(t, days_total / len(files)) if len(t) else {"trades": 0}
    # 4P 규칙 거래당 수익률 단순 누적(%) — 수수료 0 / 0.02% / 0.05% (편도)
    t4 = pd.concat(all_trades["fourp"]).sort_values("i_out")
    g = t4["gross"].to_numpy()
    step = max(1, len(g) // 300)
    res["curve_4p"] = {f"fee{fee}": [round(float(x), 2) for x in (np.cumsum(g - 2 * fee) * 100)[::step]]
                       for fee in (0.0, 0.0002, 0.0005)}
    res["curve_4p"]["step"] = step
    frames = [load(os.path.join(data_dir, f)) for f in files]
    res["sensitivity"] = sensitivity(frames, 20 * 24 * 60)
    with open(os.path.join(out_dir, "results.json"), "w") as fh:
        json.dump(res, fh, ensure_ascii=False, indent=1, default=float)
    print(json.dumps(res["pooled"], ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
