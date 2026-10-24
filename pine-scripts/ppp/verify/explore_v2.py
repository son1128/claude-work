"""PPP 2.0 설계를 위한 탐색 실험.

문서 규칙(v1)이 왜 지는지, 어떤 방향으로 고치면 나아지는지 확인합니다.
최적화가 아니라 "방향 확인"용이며, 설정값은 미리 정한 값 하나씩만 씁니다.

사용법:
    python fetch_klines.py DATA1M 75 BTCUSDT,...           # 1분봉 (run_verify.py와 공용)
    python explore_v2.py DATA1M DATA5M OUT_DIR [DATA4H]    # DATA5M/<SYM>_5m.csv 필요

실험
  E1  비용 대비 변동폭: 타임프레임별 ATR(14) / 가격 vs 왕복 수수료
  E2  타임프레임: 같은 4P 규칙을 1분·5분·15분봉에
  E3  청산 방식: 문서 청산 vs ATR 브래킷(손절 = 터치 EMA − 0.5 ATR, 목표 1.5R, 48봉 시간 손절)
  E4  이평 꼬임 필터(문서 요약서 7쪽): EMA 60·120·240 폭 < 1 ATR 이면 진입 안 함
  E5  비용 관문: 목표까지 거리 < 왕복 비용 × 4 이면 진입 안 함
  E6  트리거의 가치: 같은 1P·2P 조건에서 PPP 크로스 대신 무작위 진입 / 되찾기 양봉 / 목표 3R
  E7  1P 단독: 1시간·4시간봉 EMA 60·120·240 정배열이면 롱, 역배열이면 숏, 나머지 관망으로 보유
  E8  E7(4시간봉)을 약 7년치로 연도별 확인 (DATA4H/<SYM>_4h.csv, 선택)
"""
import json
import os
import sys

import numpy as np
import pandas as pd

import ppp_core as pc
from run_verify import load, resample

SYMS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT"]
FEES = (0.0, 0.0002, 0.0005)


def atr(df, n=14):
    h, l, c = df["high"].to_numpy(), df["low"].to_numpy(), df["close"].to_numpy()
    pc_ = np.roll(c, 1); pc_[0] = c[0]
    tr = np.maximum(h - l, np.maximum(np.abs(h - pc_), np.abs(l - pc_)))
    return pc.pine_rma(tr, n)


def bracket_backtest(df, osc, sig, tangle=False, cost_gate=None, rr=1.5, atr_mult=0.5, max_bars=48):
    """ATR 브래킷 청산. 같은 봉에서 손절·목표가 모두 닿으면 손절로 봅니다(보수적)."""
    o, h, l, c = (df[x].to_numpy() for x in ("open", "high", "low", "close"))
    a = atr(df)
    emas, touched = sig["emas"], sig["touched"]
    spread = np.max(emas, axis=0) - np.min(emas, axis=0)
    trades = []
    pos, i_in, px, stop, tgt = 0, 0, 0.0, 0.0, 0.0
    for i in range(len(c)):
        if pos != 0:
            hit_s = l[i] <= stop if pos > 0 else h[i] >= stop
            hit_t = h[i] >= tgt if pos > 0 else l[i] <= tgt
            if hit_s:
                ex = min(o[i], stop) if pos > 0 else max(o[i], stop)
                trades.append((i_in, i, pos, px, ex, "손절"))
                pos = 0
            elif hit_t:
                ex = max(o[i], tgt) if pos > 0 else min(o[i], tgt)
                trades.append((i_in, i, pos, px, ex, "목표"))
                pos = 0
            elif i - i_in >= max_bars:
                trades.append((i_in, i, pos, px, c[i], "시간"))
                pos = 0
            continue
        if np.isnan(a[i]):
            continue
        if tangle and spread[i] < a[i]:
            continue
        for side, key in ((1, "long"), (-1, "short")):
            if not sig[key][i]:
                continue
            cands = [emas[j][i] for j in range(3) if touched[j][i] and (c[i] - emas[j][i]) * side > 0]
            ref = (max(cands) if side > 0 else min(cands)) if cands else emas[0][i]
            st = ref - side * atr_mult * a[i]
            risk = (c[i] - st) * side
            if risk <= 0:
                continue
            tg = c[i] + side * rr * risk
            if cost_gate is not None and rr * risk / c[i] < cost_gate:
                continue
            pos, i_in, px, stop, tgt = side, i, c[i], st, tg
            break
    t = pd.DataFrame(trades, columns=["i_in", "i_out", "side", "px_in", "px_out", "reason"])
    if len(t):
        t["gross"] = t["side"] * (t["px_out"] / t["px_in"] - 1)
        t["bars"] = t["i_out"] - t["i_in"]
    return t


def doc_backtest(df, osc, sig, tangle=False):
    if tangle:
        a = atr(df)
        spread = np.max(sig["emas"], axis=0) - np.min(sig["emas"], axis=0)
        ok = ~(spread < a)
        sig = dict(sig, long=sig["long"] & ok, short=sig["short"] & ok)
    return pc.backtest(df, osc, sig, fee=0.0)


def stats(t, days):
    if not len(t):
        return {"trades": 0}
    g = t["gross"].to_numpy()
    out = {"trades": int(len(t)), "per_day": len(t) / days, "win_gross": float((g > 0).mean()),
           "avg_gross_bp": float(g.mean() * 1e4), "avg_bars": float(t["bars"].mean())}
    for fee in FEES:
        n = g - 2 * fee
        w, ls = n[n > 0], n[n <= 0]
        out[f"pf_{fee}"] = float(w.sum() / -ls.sum()) if ls.sum() < 0 else float("inf")
        out[f"exp_bp_{fee}"] = float(n.mean() * 1e4)
    return out


def run_tf(frames, tf_name, warm_bars, bars_per_day, halves=False):
    variants = {
        "v1 문서 규칙": lambda df, o, s: doc_backtest(df, o, s),
        "v1 + 꼬임 필터": lambda df, o, s: doc_backtest(df, o, s, tangle=True),
        "ATR 브래킷": lambda df, o, s: bracket_backtest(df, o, s),
        "ATR 브래킷 + 꼬임 필터": lambda df, o, s: bracket_backtest(df, o, s, tangle=True),
        "ATR 브래킷 + 꼬임 + 비용 관문": lambda df, o, s: bracket_backtest(df, o, s, tangle=True, cost_gate=4 * 0.001),
    }
    res = {}
    for name, fn in variants.items():
        parts = {"전체": [], "앞 절반": [], "뒤 절반": []}
        days = {"전체": 0.0, "앞 절반": 0.0, "뒤 절반": 0.0}
        for df in frames:
            osc = pc.ppp_oscillator(df["close"].to_numpy())
            sig = pc.four_p_signals(df, osc)
            cut = lambda d: {k: (v[warm_bars:] if isinstance(v, np.ndarray) else [x[warm_bars:] for x in v]) for k, v in d.items()}
            test = df.iloc[warm_bars:].reset_index(drop=True)
            t = fn(test, cut(osc), cut(sig))
            n = len(test)
            days["전체"] += n / bars_per_day
            days["앞 절반"] += n / 2 / bars_per_day
            days["뒤 절반"] += n / 2 / bars_per_day
            if len(t):
                parts["전체"].append(t)
                parts["앞 절반"].append(t[t["i_in"] < n // 2])
                parts["뒤 절반"].append(t[t["i_in"] >= n // 2])
        keys = ("전체", "앞 절반", "뒤 절반") if halves else ("전체",)
        res[name] = {k: stats(pd.concat(parts[k]) if parts[k] else pd.DataFrame(), days[k] / len(frames)) for k in keys}
        r = res[name]["전체"]
        print(tf_name, name, r.get("trades"), round(r.get("win_gross", 0), 3), round(r.get("pf_0.0", 0), 3),
              round(r.get("pf_0.0002", 0), 3), round(r.get("pf_0.0005", 0), 3), flush=True)
    return res


def trigger_value(frames, tf_name, warm_bars, bars_per_day, seed=7):
    """1P·2P는 같게 두고 3P 트리거만 바꿔서 PPP 크로스가 무작위보다 나은지 봅니다."""
    rng = np.random.default_rng(seed)
    res = {}
    rows = {"PPP 크로스": [], "무작위 (같은 빈도)": [], "되찾기 양봉/음봉": [], "PPP 크로스, 목표 3R": []}
    days = 0.0
    for df in frames:
        c, o, h, l = (df[x].to_numpy() for x in ("close", "open", "high", "low"))
        osc = pc.ppp_oscillator(c)
        sig = pc.four_p_signals(df, osc)
        ones = np.ones(len(c), bool)
        base = pc.four_p_signals(df, dict(gc=ones, dc=ones, gc_strong=ones, dc_strong=ones))
        p_l = sig["long"].sum() / max(1, base["long"].sum())
        p_s = sig["short"].sum() / max(1, base["short"].sum())
        rand = dict(base, long=base["long"] & (rng.random(len(c)) < p_l), short=base["short"] & (rng.random(len(c)) < p_s))
        h1, l1 = np.roll(h, 1), np.roll(l, 1)
        recl = dict(base, long=base["long"] & (c > o) & (c > h1), short=base["short"] & (c < o) & (c < l1))
        cut = lambda d: {k: (v[warm_bars:] if isinstance(v, np.ndarray) else [x[warm_bars:] for x in v]) for k, v in d.items()}
        test = df.iloc[warm_bars:].reset_index(drop=True)
        days += len(test) / bars_per_day
        oc = cut(osc)
        rows["PPP 크로스"].append(bracket_backtest(test, oc, cut(sig), tangle=True))
        rows["무작위 (같은 빈도)"].append(bracket_backtest(test, oc, cut(rand), tangle=True))
        rows["되찾기 양봉/음봉"].append(bracket_backtest(test, oc, cut(recl), tangle=True))
        rows["PPP 크로스, 목표 3R"].append(bracket_backtest(test, oc, cut(sig), tangle=True, rr=3.0, max_bars=96))
    for k, lst in rows.items():
        res[k] = stats(pd.concat([t for t in lst if len(t)]), days / len(frames))
        r = res[k]
        print(tf_name, "E6", k, r["trades"], round(r["win_gross"], 3), round(r["pf_0.0"], 3), round(r["pf_0.0002"], 3), flush=True)
    return res


def direction_only(frames_by_tf):
    """1P(큰 봉 방향)만으로 보유했을 때. 다음 봉 수익률에 직전 봉 방향을 곱합니다."""
    res = {}
    for tf, frames in frames_by_tf.items():
        out = []
        for df in frames:
            c = df["close"].to_numpy()
            e = [pc.pine_ema(c, n) for n in (60, 120, 240)]
            d = np.where((e[0] > e[1]) & (e[1] > e[2]), 1, np.where((e[0] < e[1]) & (e[1] < e[2]), -1, 0))[240:]
            r = np.diff(np.log(c[240:]))
            pos = d[:-1]
            turns = np.abs(np.diff(np.concatenate([[0], pos])))
            gross = float(np.sum(pos * r))
            out.append({"gross_logret": gross, "switches": int(turns.sum()),
                        "net_logret_taker": gross - turns.sum() * 0.0005, "buy_hold": float(np.log(c[-1] / c[240])),
                        "time_in_market": float((pos != 0).mean())})
        res[tf] = out
        print("E7", tf, [round(o["gross_logret"], 2) for o in out], [round(o["net_logret_taker"], 2) for o in out],
              [round(o["buy_hold"], 2) for o in out], flush=True)
    return res


def direction_by_year(d4):
    """4시간봉 1P 단독 전략의 연도별 로그수익 (수수료 편도 0.05% 반영) vs 보유."""
    res = {}
    for sym in SYMS:
        df = load(os.path.join(d4, f"{sym}_4h.csv"))
        c = df["close"].to_numpy()
        e = [pc.pine_ema(c, n) for n in (60, 120, 240)]
        d = np.where((e[0] > e[1]) & (e[1] > e[2]), 1, np.where((e[0] < e[1]) & (e[1] < e[2]), -1, 0))
        r = np.concatenate([[0.0], np.diff(np.log(c))])
        pos = np.concatenate([[0], d[:-1]])               # 직전 봉 마감 기준 방향으로 다음 봉 보유
        pos[:240] = 0
        cost = np.abs(np.diff(np.concatenate([[0], pos]))) * 0.0005
        strat = pos * r - cost
        long_only = np.where(pos > 0, r, 0.0) - np.abs(np.diff(np.concatenate([[0], np.maximum(pos, 0)]))) * 0.0005
        yr = pd.to_datetime(df["time"], unit="ms").dt.year.to_numpy()
        rows = {}
        for y in np.unique(yr[240:]):
            m = (yr == y) & (np.arange(len(c)) >= 240)
            eq = np.cumsum(strat[m])
            rows[int(y)] = {"strat": float(strat[m].sum()), "long_only": float(long_only[m].sum()),
                            "short_part": float(np.where(pos[m] < 0, pos[m] * r[m], 0).sum()),
                            "buy_hold": float(r[m].sum()), "mdd_log": float((eq - np.maximum.accumulate(np.concatenate([[0], eq]))[1:]).min())}
        st, b = strat[240:], r[240:]
        dd = lambda x: float((np.exp(np.cumsum(x) - np.maximum.accumulate(np.concatenate([[0], np.cumsum(x)]))[1:]) - 1).min())
        sharpe = lambda x: float(x.mean() / x.std() * np.sqrt(6 * 365))
        rows["overall"] = {"multiple": float(np.exp(st.sum())), "multiple_buy_hold": float(np.exp(b.sum())),
                           "mdd": dd(st), "mdd_buy_hold": dd(b), "sharpe": sharpe(st), "sharpe_buy_hold": sharpe(b),
                           "long_share": float((pos[240:] > 0).mean()), "short_share": float((pos[240:] < 0).mean())}
        res[sym] = rows
        print("E8", sym, {y: round(v["strat"], 2) for y, v in rows.items() if y != "overall"}, flush=True)
    return res


def main(d1, d5, out, d4=None):
    os.makedirs(out, exist_ok=True)
    res = {"E1_cost_vs_atr": {}}
    m1 = [load(os.path.join(d1, f"{s}_1m.csv")) for s in SYMS]
    m5 = [load(os.path.join(d5, f"{s}_5m.csv")) for s in SYMS]
    m15 = [resample(df, "15min") for df in m5]
    h1 = [resample(df, "60min") for df in m5]
    for name, frames in (("1m", m1), ("5m", m5), ("15m", m15), ("1h", h1)):
        r = [float(np.nanmedian(atr(df)[300:] / df["close"].to_numpy()[300:])) for df in frames]
        res["E1_cost_vs_atr"][name] = {"atr_pct_median": float(np.median(r)) * 100,
                                       "fee_rt_over_atr_taker": 0.001 / float(np.median(r)),
                                       "fee_rt_over_atr_maker": 0.0004 / float(np.median(r))}
    print(json.dumps(res["E1_cost_vs_atr"], indent=1))
    res["E2_1m"] = run_tf(m1, "1m", 20 * 1440, 1440)
    res["E2_5m"] = run_tf(m5, "5m", 20 * 288, 288, halves=True)
    res["E2_15m"] = run_tf(m15, "15m", 20 * 96, 96, halves=True)
    res["E6_5m"] = trigger_value(m5, "5m", 20 * 288, 288)
    res["E6_15m"] = trigger_value(m15, "15m", 20 * 96, 96)
    res["E7"] = direction_only({"1h": h1, "4h": [resample(df, "240min") for df in m5]})
    if d4:
        res["E8"] = direction_by_year(d4)
    res["period_5m"] = [str(pd.to_datetime(m5[0]["time"].iloc[i], unit="ms").date()) for i in (20 * 288, -1)]
    with open(os.path.join(out, "explore_v2.json"), "w") as fh:
        json.dump(res, fh, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main(*sys.argv[1:5])
