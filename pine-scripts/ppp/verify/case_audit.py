"""「PPP 타점 사례 모음집」 10건을 실제 거래소 시세로 되짚어 봅니다.

사용법: python case_audit.py CACHE_DIR OUT_DIR

사례마다
  1) 수익 카드의 진입가·청산가가 실제 시세 어디에 있었는지 찾고 (카드 시각은 한국시간으로 가정)
  2) 진입 시점에 문서가 말한 조건(장기 이평 위치, PPP 크로스, 1P 방향)이 실제로 있었는지
  3) 손절 기준(이평 종가 이탈)이 레버리지 청산가보다 가까웠는지
  4) 같은 코인·같은 시간봉에서 같은 조건이 며칠 동안 몇 번 나왔고, 매번 같은 목표·손절로 들어갔다면 어땠는지(기저율)
를 계산합니다.
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

import exch_data as ex
import ppp_core as pc

KST = timezone(timedelta(hours=9))
TAKER = 0.00075          # Gate 무기한 선물 시장가 수수료 (contracts API 기준)

# side: +1 롱, -1 숏 / tf: 진입 시간봉 / ma: 손절 기준 (시간봉, EMA 길이, None이면 가장 가까운 장기 이평)
CASES = [
    dict(no=1, sym="COW", src="bingx", tf="15m", side=-1, lev=48, entry=0.1321, exit=0.1259, card="2026-08-16 15:26", ma=("15m", 60), roi=224.36, who="강사"),
    dict(no=2, sym="HOLO", src="bingx", tf="1m", side=-1, lev=20, entry=0.09092, exit=0.08743, card="2026-08-12 13:47", ma=("1m", None), roi=76.75, who="강사"),
    dict(no=3, sym="SOXL", src="gate", tf="15m", side=1, lev=20, entry=134.40, exit=138.32, card="2026-08-07 00:49", ma=("15m", 120), roi=58.26, who="강사",
         note="1분봉 데이터가 없어 15분봉으로 대신 확인"),
    dict(no=4, sym="ALLO", src="bingx", tf="15m", side=1, lev=24, entry=0.40944, exit=0.42495, card="2026-06-07 08:57", ma=("15m", 60), roi=90.53, who="강사"),
    dict(no=5, sym="BEAT", src="bingx", tf="1m", side=1, lev=30, entry=5.7940, exit=5.9064, card="2026-06-11 01:04", ma=("1m", 240), roi=58.22, who="강사"),
    dict(no=6, sym="LSK", src="bingx", tf="1h", side=1, lev=50, entry=0.13888, exit=0.16491, card="2026-09-12 09:44", ma=("1d", 240), roi=937.05, who="수강생"),
    dict(no=7, sym="FF", src="bingx", tf="15m", side=1, lev=20, entry=0.14720, exit=0.16768, card="2026-09-15 13:43", ma=("15m", 240), roi=278.26, who="수강생",
         note="청산 카드가 없고, 문서의 평가가 0.16768을 청산가로 사용"),
    dict(no=8, sym="ROBO", src="bingx", tf="1m", side=1, lev=47, entry=0.01902, exit=0.01968, card="2026-08-15 16:48", ma=("1m", 60), roi=162.26, who="수강생"),
    dict(no=9, sym="4", src="bingx", tf="1m", side=-1, lev=20, entry=0.011555, exit=0.010862, card="2026-07-06 10:32", ma=("1d", 120), roi=116.11, who="수강생"),
    dict(no=10, sym="WLD", src="bingx", tf="5m", side=1, lev=33, entry=0.3892, exit=0.4025, card="2026-07-11 16:46", ma=("5m", None), roi=113.21, who="수강생"),
]
RULE = {"1m": "1min", "5m": "5min", "15m": "15min", "1h": "60min", "4h": "240min", "1d": "1D"}


def resample(df, tf):
    if tf == "1m":
        return df.copy()
    t = pd.to_datetime(df["time"], unit="ms")
    g = df.set_index(t).resample(RULE[tf], label="left", closed="left")
    out = pd.DataFrame({"open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(),
                        "close": g["close"].last(), "volume": g["volume"].sum()}).dropna()
    out["time"] = (out.index - pd.Timestamp(0)) // pd.Timedelta(milliseconds=1)
    return out.reset_index(drop=True)


def align(c):
    e = [pc.pine_ema(c, n) for n in (60, 120, 240)]
    return np.where((e[0] > e[1]) & (e[1] > e[2]), 1, np.where((e[0] < e[1]) & (e[1] < e[2]), -1, 0))


def htf_state(df_htf, t_ms, tf_ms):
    """t_ms 시점에 확정된 마지막 상위 봉의 값 인덱스."""
    idx = np.searchsorted(df_htf["time"].to_numpy(), t_ms - tf_ms, side="right") - 1
    return idx


def load_case(c, cache):
    anchor = datetime.strptime(c["card"], "%Y-%m-%d %H:%M").replace(tzinfo=KST).astimezone(timezone.utc)
    a_ms = int(anchor.timestamp() * 1000)
    start = a_ms - 13 * 86_400_000
    end = a_ms + 2 * 86_400_000
    if c["src"] == "bingx":
        m1 = ex.cached(os.path.join(cache, f"{c['sym']}_1m_{c['card'][:10]}.csv"), ex.bingx, c["sym"], "1m", start, end)
        d1 = ex.cached(os.path.join(cache, f"{c['sym']}_1d.csv"), ex.bingx, c["sym"], "1d", a_ms - 400 * 86_400_000, end)
        h4 = ex.cached(os.path.join(cache, f"{c['sym']}_4h_{c['card'][:10]}.csv"), ex.bingx, c["sym"], "4h", a_ms - 70 * 86_400_000, end)
        base = m1
    else:
        base = ex.cached(os.path.join(cache, f"{c['sym']}_15m_{c['card'][:10]}.csv"), ex.gate, c["sym"], "15m", start, end)
        d1 = ex.cached(os.path.join(cache, f"{c['sym']}_1d_gate.csv"), ex.gate, c["sym"], "1d", a_ms - 400 * 86_400_000, end)
        h4 = ex.cached(os.path.join(cache, f"{c['sym']}_4h_gate_{c['card'][:10]}.csv"), ex.gate, c["sym"], "4h", a_ms - 70 * 86_400_000, end)
    return a_ms, base, d1, h4


def locate(base, c, a_ms):
    """청산 시각(카드 ±8시간)과 진입 시각(청산 전 36시간 안 마지막 진입가 터치)을 찾습니다."""
    t = base["time"].to_numpy()
    lo, hi = base["low"].to_numpy(), base["high"].to_numpy()
    hits = np.flatnonzero((lo <= c["exit"]) & (hi >= c["exit"]) & (np.abs(t - a_ms) <= 8 * 3_600_000))
    if not len(hits):
        return None, None
    # 카드 시각 직전(또는 가장 가까운) 청산가 터치
    before = hits[t[hits] <= a_ms + 120_000]
    i_exit = before[-1] if len(before) else hits[np.argmin(np.abs(t[hits] - a_ms))]
    win = np.flatnonzero((lo[:i_exit] <= c["entry"]) & (hi[:i_exit] >= c["entry"]) & (t[:i_exit] >= t[i_exit] - 36 * 3_600_000))
    if not len(win):
        return None, i_exit
    return int(win[-1]), int(i_exit)


def stop_level(c, frames, t_ms):
    tf, n = c["ma"]
    df = frames[tf]
    i = htf_state(df, t_ms, ex.MS[tf]) if tf in ("1d", "4h", "1h") else np.searchsorted(df["time"].to_numpy(), t_ms, side="right") - 1
    cl = df["close"].to_numpy()
    if n is None:
        cands = []
        for k in (60, 120, 240):
            v = pc.pine_ema(cl, k)[i]
            if (c["side"] < 0 and v > c["entry"]) or (c["side"] > 0 and v < c["entry"]):
                cands.append((abs(v - c["entry"]), v, k))
        if not cands:
            return None, None
        _, v, n = min(cands)
        return float(v), n
    return float(pc.pine_ema(cl, n)[i]), n


def audit(c, cache):
    a_ms, base, d1, h4 = load_case(c, cache)
    r = {k: c[k] for k in ("no", "sym", "tf", "side", "lev", "entry", "exit", "roi", "who")}
    r["note"] = c.get("note", "")
    r["roi_check"] = c["lev"] * c["side"] * (c["exit"] / c["entry"] - 1) * 100
    i_in, i_out = locate(base, c, a_ms)
    if i_in is None:
        r["found"] = False
        r["price_range_near_card"] = [float(base["low"].min()), float(base["high"].max())]
        return r
    r["found"] = True
    # 청산 봉 안에 진입가도 들어 있으면 "같은 봉 안에서 진입·청산"이라는 다른 해석도 가능
    r["entry_in_exit_bar"] = bool(base["low"][i_out] <= c["entry"] <= base["high"][i_out])
    t_in, t_out = int(base["time"][i_in]), int(base["time"][i_out])
    r["entry_time_kst"] = datetime.fromtimestamp(t_in / 1000, KST).strftime("%m-%d %H:%M")
    r["exit_time_kst"] = datetime.fromtimestamp(t_out / 1000, KST).strftime("%m-%d %H:%M")
    r["hold_min"] = (t_out - t_in) / 60000
    frames = {tf: resample(base, tf) for tf in ("1m", "5m", "15m", "1h", "4h") if ex.MS[tf] >= ex.MS["1m" if c["src"] == "bingx" else "15m"]}
    frames["1d"] = d1
    frames["4h"] = h4
    # 진입~청산 사이 최대 역행(MAE)·순행(MFE)
    seg = base.iloc[i_in:i_out + 1]
    if c["side"] > 0:
        mae = seg["low"].min() / c["entry"] - 1
        mfe = seg["high"].max() / c["entry"] - 1
    else:
        mae = -(seg["high"].max() / c["entry"] - 1)
        mfe = -(seg["low"].min() / c["entry"] - 1)
    r["mae_pct"], r["mfe_pct"] = mae * 100, mfe * 100
    r["mae_roe"] = mae * c["lev"] * 100
    # 청산 후 6시간
    after = base[(base["time"] > t_out) & (base["time"] <= t_out + 6 * 3_600_000)]
    if len(after):
        r["after6h_best_pct"] = float((after["high"].max() / c["entry"] - 1) * 100 if c["side"] > 0 else -(after["low"].min() / c["entry"] - 1) * 100)
        r["after6h_worst_pct"] = float((after["low"].min() / c["entry"] - 1) * 100 if c["side"] > 0 else -(after["high"].max() / c["entry"] - 1) * 100)
    # 진입 시간봉 PPP 상태 (진입 봉 직전에 확정된 봉 기준)
    f = frames[c["tf"]]
    osc = pc.ppp_oscillator(f["close"].to_numpy())
    j = int(np.searchsorted(f["time"].to_numpy(), t_in, side="right") - 1)
    jc = j
    cross = osc["gc"] if c["side"] > 0 else osc["dc"]
    back = np.flatnonzero(cross[max(0, jc - 12):jc + 1])
    r["ppp_bars_since_cross"] = None
    if len(back):
        r["ppp_bars_since_cross"] = int(jc - (max(0, jc - 12) + back[-1]))
        r["ppp_cross_level"] = float(osc["lvl"][max(0, jc - 12) + back[-1]])
    r["ppp_k"] = float(osc["k"][jc])
    r["ppp_state"] = int(osc["state"][jc])
    # 장기 이평과의 거리 (진입 시간봉)
    cl = f["close"].to_numpy()
    atr = pc.pine_rma(np.maximum(f["high"] - f["low"], np.abs(f["high"] - np.roll(cl, 1))).to_numpy(), 14)
    dist = {}
    for n in (60, 120, 240):
        e = pc.pine_ema(cl, n)[j]
        dist[n] = float((c["entry"] - e) / atr[j]) if atr[j] > 0 else None
    r["ma_dist_atr"] = dist
    # 1P(1시간봉)·L1(4시간봉) 배열
    for tf, key in (("1h", "align_1h"), ("4h", "align_4h")):
        if tf in frames:
            h = frames[tf]
            k = htf_state(h, t_in, ex.MS[tf])
            r[key] = int(align(h["close"].to_numpy())[k]) if k >= 239 else None
    k = htf_state(d1, t_in, ex.MS["1d"])
    r["align_1d"] = int(align(d1["close"].to_numpy())[k]) if k >= 239 else None
    # 손절 기준과 청산가
    sv, sn = stop_level(c, frames, t_in)
    r["stop_ma"] = f"{c['ma'][0]} EMA{sn}" if sn else None
    r["stop_level"] = sv
    r["stop_dist_pct"] = (c["side"] * (c["entry"] - sv) / c["entry"] * 100) if sv else None
    mmr = 0.01
    r["liq_dist_pct"] = (1 / c["lev"] - mmr) * 100       # 격리 마진 근사
    r["stop_beyond_liq"] = bool(r["stop_dist_pct"] is not None and r["stop_dist_pct"] > r["liq_dist_pct"])
    r["max_lev_for_stop"] = (1 / (r["stop_dist_pct"] / 100 + mmr)) if r["stop_dist_pct"] and r["stop_dist_pct"] > 0 else None
    r["fee_roe_pct"] = 2 * TAKER * c["lev"] * 100
    # 증거금이 계좌 전체(교차 마진)라고 볼 때 손절 한 번에 잃는 계좌 비율
    r["acct_risk_pct"] = min(100.0, c["lev"] * r["stop_dist_pct"]) if r["stop_dist_pct"] and r["stop_dist_pct"] > 0 else None
    r["base_rate"] = base_rate(c, frames, i_tf=j)
    return r


def base_rate(c, frames, i_tf, look=7 * 96):
    """같은 시간봉에서 같은 조건(장기 이평 근처 + 같은 방향 PPP 크로스)이 나올 때마다
    같은 목표(%)·같은 레버리지로 들어갔다면. 손절 = 기준 EMA 종가 이탈, 청산 = 가격이 청산 거리만큼 역행."""
    tf = c["tf"]
    f = frames[tf]
    cl, hi, lo = (f[x].to_numpy() for x in ("close", "high", "low"))
    osc = pc.ppp_oscillator(cl)
    n_ma = c["ma"][1] if c["ma"][0] == tf and c["ma"][1] else None
    emas = [pc.pine_ema(cl, n) for n in ((n_ma,) if n_ma else (60, 120, 240))]
    atr = pc.pine_rma(np.maximum(hi - lo, np.abs(hi - np.roll(cl, 1))), 14)
    cross = osc["gc"] if c["side"] > 0 else osc["dc"]
    tgt = abs(c["exit"] / c["entry"] - 1)
    liq = 1 / c["lev"] - 0.01
    out = []
    start = max(240, i_tf - look if tf != "1m" else i_tf - 3 * 1440)
    for i in range(start, len(cl) - 1):
        if not cross[i]:
            continue
        ok = None
        for e in emas:
            near = np.any((lo[max(0, i - 5):i + 1] <= e[i] + 0.5 * atr[i]) & (hi[max(0, i - 5):i + 1] >= e[i] - 0.5 * atr[i]))
            right_side = (cl[i] - e[i]) * c["side"] > 0
            if near and right_side:
                ok = e
                break
        if ok is None:
            continue
        px = cl[i]
        res = None
        for k in range(i + 1, min(len(cl), i + 500)):
            adverse = (lo[k] / px - 1) * c["side"] if c["side"] > 0 else (hi[k] / px - 1) * c["side"]
            favor = (hi[k] / px - 1) * c["side"] if c["side"] > 0 else (lo[k] / px - 1) * c["side"]
            if adverse <= -liq:
                res = ("청산", -100.0)
                break
            if favor >= tgt:
                res = ("목표", tgt * c["lev"] * 100)
                break
            if (cl[k] - ok[k]) * c["side"] < 0:
                res = ("손절", (cl[k] / px - 1) * c["side"] * c["lev"] * 100)
                break
        if res is None:
            continue
        out.append((int(f["time"][i]), res[0], res[1] - 2 * TAKER * c["lev"] * 100))
    if not out:
        return {"n": 0}
    roe = np.array([x[2] for x in out])
    kinds = pd.Series([x[1] for x in out]).value_counts().to_dict()
    return {"n": len(out), "days": round((len(cl) - start) * ex.MS[tf] / 86_400_000, 1), "target": kinds.get("목표", 0), "stop": kinds.get("손절", 0),
            "liq": kinds.get("청산", 0), "win_rate": float((roe > 0).mean()), "avg_roe": float(roe.mean()), "sum_roe": float(roe.sum())}


def main(cache, out):
    os.makedirs(out, exist_ok=True)
    res = []
    for c in CASES:
        try:
            r = audit(c, cache)
        except Exception as e:                       # 거래소에 없는 종목 등
            r = {"no": c["no"], "sym": c["sym"], "error": str(e)}
        res.append(r)
        print(json.dumps(r, ensure_ascii=False, default=float), flush=True)
    with open(os.path.join(out, "case_audit.json"), "w") as fh:
        json.dump(res, fh, ensure_ascii=False, indent=1, default=float)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
