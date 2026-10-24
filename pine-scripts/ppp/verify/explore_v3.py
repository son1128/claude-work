"""「PPP 타점 사례 모음집」에서 나온 개선안을 검증합니다 (E9~E13).

사용법: python explore_v3.py DATA5M_MAJORS ALTS_DIR OUT_DIR
  DATA5M_MAJORS/<SYM>_5m.csv  : 바이낸스 현물 BTC·ETH·SOL·XRP·DOGE 5분봉 (explore_v2.py와 공용)
  ALTS_DIR/<SYM>_5m.csv       : BingX 무기한 선물 사례 코인 5분봉 (exch_data.bingx)

E9   구조적 손절 거리 vs 레버리지 청산 거리
E10  크로스 품질 점수 구간별 성과 (점수가 성과를 가려내는가)
E11  청산 방식: 고정 1.5R vs 분할 익절·본전·추적 청산
E12  상위 시간봉(4시간) 방향 일치 여부
E13  다이버전스 동반 여부
E14  권장 조합(L1 방향 일치 + 2P 근처 크로스 + 분할 청산) vs 같은 조건의 무작위 진입
"""
import json
import os
import sys

import numpy as np
import pandas as pd

import ppp_v2 as v2
from run_verify import load, resample

MAJORS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT"]
ALTS = ["COW", "HOLO", "ALLO", "BEAT", "LSK", "FF", "ROBO", "4", "WLD"]
LEVS = (10, 20, 30, 50)
WARM = 300


def universe(paths):
    out = {}
    for name, p in paths.items():
        if os.path.exists(p):
            df = load(p)
            if len(df) > 3000:
                out[name] = df
    return out


def collect(frames, tf):
    """시간봉별로 특징을 한 번만 계산해 둡니다."""
    res = []
    for name, df5 in frames.items():
        df = df5 if tf == "5m" else resample(df5, "15min")
        f = v2.features(df)
        res.append((name, df, f))
    return res


def run(items, pick, mode="bracket"):
    ts = []
    for name, df, f in items:
        el, es = pick(f)
        el[:WARM] = False
        es[:WARM] = False
        t = v2.backtest(df, f, el, es, mode=mode)
        if len(t):
            ts.append(t.assign(sym=name))
    return pd.concat(ts) if ts else pd.DataFrame()


def e9(items):
    d = []
    for name, df, f in items:
        o = f["osc"]
        c = df["close"].to_numpy()
        idx = np.flatnonzero((o["gc"] & f["near_l"]) | (o["dc"] & f["near_s"]))
        idx = idx[idx > WARM]
        for i in idx:
            side = 1 if o["gc"][i] else -1
            st = v2.structural_stop(f, i, side, c[i])
            d.append(abs(c[i] - st) / c[i])
    d = np.array(d)
    out = {"n": int(len(d)), "median_pct": float(np.median(d) * 100), "p75_pct": float(np.percentile(d, 75) * 100),
           "p90_pct": float(np.percentile(d, 90) * 100), "risk1pct_leverage_median": float(np.median(0.01 / d))}
    for lev in LEVS:
        out[f"beyond_liq_{lev}x"] = float((d > v2.liq_distance(lev)).mean())
    return out


def main(maj_dir, alt_dir, out):
    os.makedirs(out, exist_ok=True)
    unis = {
        "majors": universe({s: os.path.join(maj_dir, f"{s}_5m.csv") for s in MAJORS}),
        "alts": universe({s: os.path.join(alt_dir, f"{s}_5m.csv") for s in ALTS}),
    }
    res = {"universe": {k: {n: len(v) for n, v in u.items()} for k, u in unis.items()}}
    for uname, frames in unis.items():
        for tf in ("5m", "15m"):
            key = f"{uname}_{tf}"
            items = collect(frames, tf)
            r = {"E9": e9(items)}
            # E10: 모든 크로스를 점수 구간으로 나눔
            buckets = {}
            for lo_, hi_ in ((0, 40), (40, 60), (60, 80), (80, 101)):
                pick = lambda f, lo_=lo_, hi_=hi_: (f["osc"]["gc"] & (f["score_l"] >= lo_) & (f["score_l"] < hi_),
                                                    f["osc"]["dc"] & (f["score_s"] >= lo_) & (f["score_s"] < hi_))
                buckets[f"{lo_}-{min(hi_, 100)}"] = v2.stats(run(items, pick))
            r["E10"] = buckets
            # 무작위 대조: 60점 이상과 같은 개수, 같은 방향 비율
            rng = np.random.default_rng(11)

            def rand_pick(f):
                n = len(f["atr"])
                pl = (f["osc"]["gc"] & (f["score_l"] >= 60)).mean()
                ps = (f["osc"]["dc"] & (f["score_s"] >= 60)).mean()
                return rng.random(n) < pl, rng.random(n) < ps
            r["E10_random"] = v2.stats(run(items, rand_pick))
            # E11: 60점 이상에서 청산 방식 비교
            hi_pick = lambda f: (f["osc"]["gc"] & (f["score_l"] >= 60), f["osc"]["dc"] & (f["score_s"] >= 60))
            r["E11"] = {"고정 1.5R": v2.stats(run(items, hi_pick, "bracket")),
                        "분할·본전·추적": v2.stats(run(items, hi_pick, "manager"))}
            # E12: 2P(장기 이평 근처) 크로스를 4시간봉 방향 일치 여부로 나눔
            e12 = {}
            for lab, want in (("일치", 1), ("중립", 0), ("반대", -1)):
                pick = lambda f, want=want: (f["osc"]["gc"] & f["near_l"] & (f["l1"] == want),
                                             f["osc"]["dc"] & f["near_s"] & (f["l1"] == -want))
                e12[lab] = v2.stats(run(items, pick))
            r["E12"] = e12
            # E13: 2P 크로스를 다이버전스 동반 여부로 나눔
            div_l = lambda f: v2.barssince_le(f["bull_div"], 10)
            div_s = lambda f: v2.barssince_le(f["bear_div"], 10)
            r["E13"] = {
                "다이버전스 있음": v2.stats(run(items, lambda f: (f["osc"]["gc"] & f["near_l"] & div_l(f), f["osc"]["dc"] & f["near_s"] & div_s(f)))),
                "다이버전스 없음": v2.stats(run(items, lambda f: (f["osc"]["gc"] & f["near_l"] & ~div_l(f), f["osc"]["dc"] & f["near_s"] & ~div_s(f)))),
            }
            # E14: L1 일치 + 2P 크로스 + 분할 청산, 그리고 L1 일치·2P 조건만 같게 둔 무작위 진입(5회 평균)
            rec = lambda f: (f["osc"]["gc"] & f["near_l"] & (f["l1"] == 1), f["osc"]["dc"] & f["near_s"] & (f["l1"] == -1))
            r14 = {"권장 조합": v2.stats(run(items, rec, "manager"))}
            rs = []
            for seed in range(5):
                rng2 = np.random.default_rng(100 + seed)

                def rnd(f, rng2=rng2):
                    bl = f["near_l"] & (f["l1"] == 1)
                    bs = f["near_s"] & (f["l1"] == -1)
                    pl = (f["osc"]["gc"] & bl).sum() / max(1, bl.sum())
                    ps = (f["osc"]["dc"] & bs).sum() / max(1, bs.sum())
                    n = len(f["atr"])
                    return bl & (rng2.random(n) < pl), bs & (rng2.random(n) < ps)
                rs.append(v2.stats(run(items, rnd, "manager")))
            r14["무작위 (같은 조건, 5회 평균)"] = {k: float(np.mean([x[k] for x in rs])) for k in rs[0]}
            r["E14"] = r14
            print(key, "E14", {a_: (round(b_["pf_0.0"], 3), round(b_["pf_0.0002"], 3), int(b_["trades"])) for a_, b_ in r14.items()}, flush=True)
            res[key] = r
            print(key, "E9", {k: round(v, 3) for k, v in r["E9"].items()}, flush=True)
            for b, v in r["E10"].items():
                print(key, "E10", b, v.get("trades"), round(v.get("win_gross", 0), 3), round(v.get("pf_0.0", 0), 3), round(v.get("pf_0.00075", 0), 3), flush=True)
            print(key, "E10 random", r["E10_random"].get("trades"), round(r["E10_random"].get("pf_0.0", 0), 3), flush=True)
            for b, v in r["E11"].items():
                print(key, "E11", b, v.get("trades"), round(v.get("win_gross", 0), 3), round(v.get("pf_0.0", 0), 3), round(v.get("pf_0.0002", 0), 3), round(v.get("avg_R", 0), 3), flush=True)
            for b, v in r["E12"].items():
                print(key, "E12", b, v.get("trades"), round(v.get("pf_0.0", 0), 3), flush=True)
            for b, v in r["E13"].items():
                print(key, "E13", b, v.get("trades"), round(v.get("pf_0.0", 0), 3), flush=True)
    with open(os.path.join(out, "explore_v3.json"), "w") as fh:
        json.dump(res, fh, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main(*sys.argv[1:4])
