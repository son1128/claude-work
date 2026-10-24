"""단계 2 연구 실행: python -m ppp2 -c config.toml research stage2 --out results.json

통과 기준 (기획서 단계 2)
  - 비용 후 기간 분할 검증(OOS) 포트폴리오 샤프 ≥ 0.8
  - 최대 낙폭 ≤ 25% (변동성 기준 비중 적용)
  - 검증 구간의 60% 이상에서 플러스
  - 설정 ±25%를 바꿔도 샤프 70% 이상 유지
"""
import json
import time
from dataclasses import replace

import numpy as np
import pandas as pd

from . import trend as tr

DOC = tr.TrendCfg()                      # 문서 그대로: 4시간봉 EMA 60/120/240, 롱·숏


def grid():
    cfgs = []
    for tf in ("4h", "1d"):
        for rule, lens in (("ema3", (30, 60, 120)), ("ema3", (60, 120, 240)), ("ema3", (90, 180, 360)),
                           ("ema2", (50, 100, 0)), ("ema2", (100, 200, 0))):
            for lo in (False, True):
                cfgs.append(replace(DOC, tf=tf, rule=rule, lens=lens, long_only=lo))
    return cfgs


def _curve(dr, step=7):
    eq = (1 + dr).cumprod()
    w = eq.iloc[::step]
    return {"t": [d.strftime("%Y-%m-%d") for d in w.index], "v": [round(float(x), 4) for x in w]}


def _pos_windows(dr, days=182):
    """겹치지 않는 6개월 구간 중 수익이 플러스인 비율."""
    out, t = [], dr.index[0]
    while t + pd.Timedelta(days=days) <= dr.index[-1] + pd.Timedelta(days=1):
        part = dr[(dr.index >= t) & (dr.index < t + pd.Timedelta(days=days))]
        out.append((1 + part).prod() - 1 > 0)
        t += pd.Timedelta(days=days)
    return float(np.mean(out)) if out else None


def gate_funding(symbols, http=None):
    """Gate 무기한 선물의 최근 펀딩비(최대 1,000회) 평균을 연율로. 가정(연 10%)이 맞는지 확인용."""
    from ..data.exchanges import GateFutures
    g = GateFutures(http=http)
    out = {}
    for s in symbols:
        try:
            rows = g.funding(s.split("~")[0])
        except Exception:
            continue
        if len(rows) < 30:
            continue
        ts = np.array([r[0] for r in rows])
        rates = np.array([r[1] for r in rows])
        per_year = 365 * 86_400_000 / np.median(np.diff(ts))
        out[s] = {"n": len(rows), "apr": float(rates.mean() * per_year),
                  "from": str(pd.to_datetime(ts[0], unit="ms").date())}
    return out


def benchmarks(close4h):
    c = tr.to_tf(close4h, "1d")
    r = c.pct_change(fill_method=None)
    btc = r["BTC"].fillna(0.0)
    ew = r.mean(axis=1, skipna=True).fillna(0.0)               # 살아 있는 종목 동일 비중
    always_long = replace(DOC, tf="1d")
    d = c.notna().astype(float)                                # 살아 있는 동안 항상 롱
    w = tr.apply_band(tr.target_weights(c, d, always_long), always_long.band)
    vt, _ = tr.simulate(c, w, always_long)
    to_daily = lambda s: s.set_axis(pd.to_datetime(s.index, unit="ms"))
    return {"BTC 보유": to_daily(btc), "동일 비중 보유": to_daily(ew), "변동성 기준 상시 롱": tr.daily(vt)}


def evaluate(store, exchange, symbols, out_path=None, random_reps=200, log=print, funding_check=True):
    t0 = time.time()
    close = tr.load_panel(store, exchange, symbols)
    res = {"universe": {s: [str(pd.to_datetime(close[s].first_valid_index(), unit="ms").date()),
                            str(pd.to_datetime(close[s].last_valid_index(), unit="ms").date())] for s in close.columns}}
    log(f"종목 {close.shape[1]}개, 4시간봉 {len(close)}개 ({res['universe']['BTC'][0]} ~ {res['universe']['BTC'][1]})")

    # 1) 문서 설정 그대로 (선택 과정이 없어 전 구간이 사실상 검증 구간)
    net, parts, _ = tr.run(close, DOC)
    res["doc"] = {"cfg": DOC.label(), "stats": tr.stats(net, parts), "by_year": tr.by_year(net),
                  "bootstrap": tr.block_bootstrap_sharpe(net)}
    log(f"문서 설정 {DOC.label()}: {json.dumps(res['doc']['stats'], ensure_ascii=False)[:200]}")

    # 2) 벤치마크
    bm = benchmarks(close)
    res["benchmarks"] = {k: tr.daily_stats(v[v.index >= tr.daily(net).index[0]]) for k, v in bm.items()}

    # 3) 기간 분할 검증 (설정 20개 중 학습 구간 1위를 다음 반년에)
    cfgs = grid()
    oos, windows, series = tr.walk_forward(close, cfgs)
    res["wf"] = {"stats": tr.daily_stats(oos), "windows": windows,
                 "pos_windows": float(np.mean([w["test_return"] > 0 for w in windows])),
                 "bootstrap": tr.block_bootstrap_sharpe(oos)}
    res["grid"] = [{"cfg": c.label(), **tr.daily_stats(series[c][series[c].index >= oos.index[0]])} for c in cfgs]
    log(f"기간 분할 검증: {json.dumps(res['wf']['stats'], ensure_ascii=False)}  플러스 구간 {res['wf']['pos_windows']:.0%}")

    # 4) 설정 안정성
    base_sh = res["doc"]["stats"]["sharpe"]
    res["plateau"] = []
    for name, c in tr.neighbors(DOC):
        n2, p2, _ = tr.run(close, c)
        s2 = tr.stats(n2, p2)
        res["plateau"].append({"change": name, "sharpe": s2["sharpe"], "mdd": s2["mdd"], "ratio": s2["sharpe"] / base_sh if base_sh else None})

    # 5) 비용·펀딩비·목표 변동성·롱만
    sens = []
    for name, c in (("지정가 수수료 0.02%", replace(DOC, fee=0.0002)), ("수수료 2배 0.15%", replace(DOC, fee=0.0015)),
                    ("펀딩비 0%", replace(DOC, funding_apr=0.0)), ("펀딩비 연 20%", replace(DOC, funding_apr=0.20)),
                    ("목표 변동성 20%", replace(DOC, target_vol=0.20)), ("목표 변동성 40%", replace(DOC, target_vol=0.40)),
                    ("포트폴리오 배율 조정 끔", replace(DOC, port_scale=False)), ("리밸런싱 범위 없음", replace(DOC, band=0.0)),
                    ("롱만", replace(DOC, long_only=True)), ("일봉", replace(DOC, tf="1d"))):
        n2, p2, _ = tr.run(close, c)
        sens.append({"case": name, **tr.stats(n2, p2)})
    res["sensitivity"] = sens

    # 6) 무작위 대조 (타이밍의 가치)
    rc = tr.random_control(close, DOC, reps=random_reps)
    res["random_control"] = {"reps": len(rc), "mean": float(rc.mean()), "p95": float(np.percentile(rc, 95)),
                             "p_value": float((rc >= base_sh).mean()), "actual": base_sh}
    log(f"무작위 대조: 평균 샤프 {rc.mean():.2f}, 95% {np.percentile(rc, 95):.2f}, 실제 {base_sh:.2f}")

    # 7) 추가 확인: 이전 실험(E8)에 안 쓴 종목, 기간 반으로 나누기, 6개월 단위 플러스 비율
    e8 = {"BTC", "ETH", "SOL", "XRP", "DOGE"}
    sub = close[[c for c in close.columns if c.split("~")[0] not in e8]]
    n_sub, p_sub, _ = tr.run(sub, DOC)
    halves = {}
    cut = pd.Timestamp("2023-01-01")
    dd = tr.daily(net)
    for name, part in (("2019-04 ~ 2022-12", dd[dd.index < cut]), ("2023-01 ~ 2026-10", dd[dd.index >= cut])):
        halves[name] = tr.daily_stats(part)
    res["robust"] = {"ex_e8_symbols": {"n": sub.shape[1], **tr.stats(n_sub, p_sub)}, "halves": halves,
                     "pos_half_years_doc": _pos_windows(dd)}

    # 8) 권장 후보: 길이 3세트 앙상블, 목표 변동성 20% (설정 고르기를 하지 않음)
    rec = replace(DOC, rule="ens3", target_vol=0.20)
    n_rec, p_rec, _ = tr.run(close, rec)
    rec_d = tr.daily(n_rec)
    rec_plateau = []
    base_rec = tr.stats(n_rec, p_rec)["sharpe"]
    for name, c in tr.neighbors(rec):
        n2, p2, _ = tr.run(close, c)
        s2 = tr.stats(n2, p2)
        rec_plateau.append({"change": name, "sharpe": s2["sharpe"], "mdd": s2["mdd"], "ratio": s2["sharpe"] / base_rec})
    rc2 = tr.random_control(close, rec, reps=max(50, random_reps // 2))
    n_rs, p_rs, _ = tr.run(sub, rec)
    res["recommended"] = {"cfg": "4h EMA 앙상블 (30/60/120, 60/120/240, 90/180/360) 롱·숏, 목표 변동성 20%",
                          "stats": tr.stats(n_rec, p_rec), "by_year": tr.by_year(n_rec),
                          "bootstrap": tr.block_bootstrap_sharpe(n_rec), "pos_half_years": _pos_windows(rec_d),
                          "plateau": rec_plateau, "ex_e8": tr.stats(n_rs, p_rs),
                          "random_control": {"mean": float(rc2.mean()), "p95": float(np.percentile(rc2, 95)),
                                             "p_value": float((rc2 >= base_rec).mean())},
                          "halves": {k: tr.daily_stats(v) for k, v in (("2019-04 ~ 2022-12", rec_d[rec_d.index < cut]),
                                                                       ("2023-01 ~ 2026-10", rec_d[rec_d.index >= cut]))}}
    log(f"권장 후보: {json.dumps(res['recommended']['stats'], ensure_ascii=False)[:160]}")

    # 9) 통과 기준
    wf = res["wf"]["stats"]
    plateau_min = min(p["ratio"] for p in res["plateau"] if p["ratio"] is not None)
    res["gate"] = [
        {"item": "기간 분할 검증 샤프 ≥ 0.8 (비용·펀딩비 후)", "value": wf["sharpe"], "pass": wf["sharpe"] >= 0.8},
        {"item": "최대 낙폭 ≤ 25%", "value": wf["mdd"], "pass": wf["mdd"] >= -0.25},
        {"item": "검증 구간 60% 이상 플러스", "value": res["wf"]["pos_windows"], "pass": res["wf"]["pos_windows"] >= 0.6},
        {"item": "설정 ±25%에서 샤프 70% 이상 유지 (문서 설정 기준)", "value": plateau_min, "pass": plateau_min >= 0.7},
        {"item": "무작위 대조보다 우위 (p < 0.05)", "value": res["random_control"]["p_value"], "pass": res["random_control"]["p_value"] < 0.05},
    ]
    res["gate_pass"] = all(g["pass"] for g in res["gate"])
    rr = res["recommended"]
    rplat = min(p["ratio"] for p in rr["plateau"])
    res["gate_recommended"] = [
        {"item": "샤프 ≥ 0.8 (전 구간, 설정 선택 없음, 비용·펀딩비 후)", "value": rr["stats"]["sharpe"], "pass": rr["stats"]["sharpe"] >= 0.8},
        {"item": "부트스트랩 하위 5% 샤프 > 0", "value": rr["bootstrap"]["p05"], "pass": rr["bootstrap"]["p05"] > 0},
        {"item": "최대 낙폭 ≤ 25%", "value": rr["stats"]["mdd"], "pass": rr["stats"]["mdd"] >= -0.25},
        {"item": "6개월 구간 60% 이상 플러스", "value": rr["pos_half_years"], "pass": rr["pos_half_years"] >= 0.6},
        {"item": "설정 ±25%에서 샤프 70% 이상 유지", "value": rplat, "pass": rplat >= 0.7},
        {"item": "무작위 대조보다 우위 (p < 0.05)", "value": rr["random_control"]["p_value"], "pass": rr["random_control"]["p_value"] < 0.05},
        {"item": "이전 실험에 안 쓴 31종목만으로 샤프 ≥ 0.8", "value": rr["ex_e8"]["sharpe"], "pass": rr["ex_e8"]["sharpe"] >= 0.8},
        {"item": "앞·뒤 절반 모두 샤프 ≥ 0.5", "value": min(h["sharpe"] for h in rr["halves"].values()),
         "pass": min(h["sharpe"] for h in rr["halves"].values()) >= 0.5},
    ]
    res["gate_recommended_pass"] = all(g["pass"] for g in res["gate_recommended"])

    # 8) 그림용 곡선 (주 단위)
    start = oos.index[0]
    doc_d = tr.daily(net)
    res["curves"] = {"기간 분할 검증 (OOS)": _curve(oos), "문서 설정": _curve(doc_d[doc_d.index >= start]),
                     **{k: _curve(v[v.index >= start].fillna(0.0)) for k, v in bm.items()}}
    res["curves"]["권장 후보 (앙상블 20%)"] = _curve(rec_d[rec_d.index >= start])
    res["curves_full"] = {"권장 후보 (앙상블 20%)": _curve(rec_d), "문서 설정": _curve(doc_d),
                          **{k: _curve(v.fillna(0.0)) for k, v in bm.items()}}
    eqr = (1 + rec_d).cumprod()
    res["drawdown_recommended"] = {"t": [d.strftime("%Y-%m-%d") for d in eqr.index[::7]],
                                   "v": [round(float(x), 4) for x in (eqr / eqr.cummax() - 1).iloc[::7]]}
    eq = (1 + oos).cumprod()
    res["drawdown"] = {"t": [d.strftime("%Y-%m-%d") for d in eq.index[::7]],
                       "v": [round(float(x), 4) for x in (eq / eq.cummax() - 1).iloc[::7]]}
    if funding_check:
        f = gate_funding(close.columns)
        if f:
            aprs = np.array([v["apr"] for v in f.values()])
            res["funding_check"] = {"symbols": f, "median_apr": float(np.median(aprs)), "mean_apr": float(aprs.mean())}
            log(f"Gate 실제 펀딩비 중앙값 연 {np.median(aprs)*100:.1f}% ({len(f)}종목)")
    res["elapsed_s"] = time.time() - t0
    if out_path:
        with open(out_path, "w") as fh:
            json.dump(res, fh, ensure_ascii=False, indent=1, default=float)
    return res
