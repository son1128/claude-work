"""PPP v2 계산 (ppp-oscillator-v2.pine / ppp-v2-strategy.pine 와 같은 정의).

v1(ppp_core)의 오실레이터는 그대로 두고, 「PPP 타점 사례 모음집」 분석에서 나온 개선을 더합니다.
  - 크로스 품질 점수 (0~100): 구간, 장기 이평 근접, 상위 시간봉 방향, K 기울기, 다이버전스, 꼬임 감점
  - 피벗 기반 다이버전스 (가격 vs K)
  - 구조적 손절가와 청산가 비교, 위험 기준 레버리지
  - 분할 익절 + 본전 이동 + 추세 추적 청산

원본: pine-scripts/ppp/verify/ppp_v2.py (tests/test_parity.py가 두 구현의 결과가 같은지 확인합니다)
"""
import numpy as np
import pandas as pd

from . import oscillator as pc


def atr(h, l, c, n=14):
    pc_ = np.roll(c, 1)
    pc_[0] = c[0]
    tr = np.maximum(h - l, np.maximum(np.abs(h - pc_), np.abs(l - pc_)))
    return pc.pine_rma(tr, n)


def htf_align(df, rule, lens=(60, 120, 240)):
    """request.security(rule, align()[1], lookahead_on): 직전에 확정된 상위 봉의 EMA 배열."""
    t = pd.to_datetime(df["time"], unit="ms")
    h = df.set_index(t)["close"].resample(rule, label="left", closed="left").last().dropna()
    e = [pc.pine_ema(h.to_numpy(), n) for n in lens]
    d = np.where((e[0] > e[1]) & (e[1] > e[2]), 1, np.where((e[0] < e[1]) & (e[1] < e[2]), -1, 0))
    s = pd.Series(d, index=h.index).shift(1)
    return s.reindex(t.dt.floor(pd.Timedelta(rule))).fillna(0).to_numpy().astype(int)


def pivots(x, left, right, low=True):
    """ta.pivotlow/pivothigh: right봉 뒤에 확정. 반환값은 '확정된 봉' 위치에 피벗 값(그 외 NaN)."""
    n = len(x)
    out = np.full(n, np.nan)
    for i in range(left + right, n):
        j = i - right
        w = x[j - left:i + 1]
        if np.isnan(w).any():
            continue
        v = x[j]
        others = np.delete(w, left)
        if (low and v < others.min()) or (not low and v > others.max()):
            out[i] = v
    return out


def divergence(k, low, high, left=5, right=2, max_gap=60):
    """정규 다이버전스. 상승: 가격 저점은 낮아지고 K 저점은 높아짐. 하락은 반대.
    값은 확정된 봉에서 True."""
    n = len(k)
    pl = pivots(k, left, right, True)
    ph = pivots(k, left, right, False)
    bull = np.zeros(n, bool)
    bear = np.zeros(n, bool)
    last_l = last_h = None                      # (봉 위치, K 값, 가격)
    for i in range(n):
        if not np.isnan(pl[i]):
            j = i - right
            cur = (j, pl[i], low[j])
            if last_l and j - last_l[0] <= max_gap and cur[2] < last_l[2] and cur[1] > last_l[1]:
                bull[i] = True
            last_l = cur
        if not np.isnan(ph[i]):
            j = i - right
            cur = (j, ph[i], high[j])
            if last_h and j - last_h[0] <= max_gap and cur[2] > last_h[2] and cur[1] < last_h[1]:
                bear[i] = True
            last_h = cur
    return bull, bear


def barssince_le(cond, win):
    out = np.zeros(len(cond), bool)
    last = -10**9
    for i, c in enumerate(cond):
        if c:
            last = i
        out[i] = (i - last) <= win
    return out


def features(df, l1_rule="240min", lens=(60, 120, 240), ob=75.0, os_=25.0, div_win=10, tangle_mult=1.0, l1=None):
    """l1을 주면(봉마다 +1/0/-1) 상위 시간봉 방향을 df에서 다시 계산하지 않고 그대로 씁니다.
    실행 엔진은 따로 수집한 4시간봉으로 계산한 값을 넘깁니다."""
    o, h, l, c = (df[x].to_numpy() for x in ("open", "high", "low", "close"))
    osc = pc.ppp_oscillator(c, ob=ob, os_=os_)
    a = atr(h, l, c)
    emas = [pc.pine_ema(c, n) for n in lens]
    lo5 = pd.Series(l).rolling(5, min_periods=1).min().to_numpy()
    hi5 = pd.Series(h).rolling(5, min_periods=1).max().to_numpy()
    near_l = np.zeros(len(c), bool)
    near_s = np.zeros(len(c), bool)
    for e in emas:
        near_l |= (lo5 <= e + 0.5 * a) & (c > e)
        near_s |= (hi5 >= e - 0.5 * a) & (c < e)
    spread = np.max(emas, axis=0) - np.min(emas, axis=0)
    tangle = spread < tangle_mult * a
    if l1 is None:
        l1 = htf_align(df, l1_rule, lens) if l1_rule else np.zeros(len(c), int)
    l1 = np.asarray(l1, dtype=int)
    bull, bear = divergence(osc["k"], l, h)
    bull_r, bear_r = barssince_le(bull, div_win), barssince_le(bear, div_win)
    k = osc["k"]
    slope = k - np.concatenate([[np.nan], k[:-1]])
    lvl = osc["lvl"]
    zone_l = np.where(lvl <= os_, 25, np.where(lvl <= os_ + 10, 12, 0))
    zone_s = np.where(lvl >= ob, 25, np.where(lvl >= ob - 10, 12, 0))
    score_l = zone_l + near_l * 25 + np.where(l1 == 1, 25, np.where(l1 == 0, 10, 0)) + (slope >= 5) * 10 + bull_r * 15 - tangle * 20
    score_s = zone_s + near_s * 25 + np.where(l1 == -1, 25, np.where(l1 == 0, 10, 0)) + (slope <= -5) * 10 + bear_r * 15 - tangle * 20
    return dict(osc=osc, atr=a, emas=emas, near_l=near_l, near_s=near_s, tangle=tangle, l1=l1,
                bull_div=bull, bear_div=bear, score_l=np.clip(score_l, 0, 100), score_s=np.clip(score_s, 0, 100))


def structural_stop(f, i, side, close, buf=0.5):
    """보호 방향에서 가장 가까운 장기 EMA ∓ buf·ATR. 없으면 EMA60."""
    es = [e[i] for e in f["emas"]]
    if side > 0:
        below = [e for e in es if e < close]
        ref = max(below) if below else es[0]
        return ref - buf * f["atr"][i]
    above = [e for e in es if e > close]
    ref = min(above) if above else es[0]
    return ref + buf * f["atr"][i]


def liq_distance(lev, mmr=0.01):
    """격리 마진 근사 청산 거리 (가격 대비 비율)."""
    return max(0.0, 1.0 / lev - mmr)


def max_leverage(stop_dist, mmr=0.01, safety=0.8):
    """손절이 청산보다 먼저 오게 하는 최대 레버리지 (여유 20%)."""
    return safety / (stop_dist + mmr)


def risk_leverage(stop_dist, risk=0.01):
    """계좌 위험을 risk로 고정할 때 필요한 명목 레버리지 = risk / 손절거리."""
    return risk / stop_dist


def backtest(df, f, entries_l, entries_s, mode="manager", rr=1.5, tp1_r=1.0, tp1_frac=0.5,
             trail_len=20, max_bars=288, pre_tp_bars=96):
    """mode = "bracket": 손절/목표(rr) 고정 / "manager": 1R에서 절반 익절 → 본전 → EMA(trail_len) 종가 이탈 시 나머지 청산.
    같은 봉에서 손절과 목표가 모두 닿으면 손절로 봅니다. 수익은 가격 기준(레버리지 미적용)."""
    o, h, l, c = (df[x].to_numpy() for x in ("open", "high", "low", "close"))
    trail = pc.pine_ema(c, trail_len)
    trades = []
    i, n = 0, len(c)
    while i < n - 1:
        side = 1 if entries_l[i] else (-1 if entries_s[i] else 0)
        if side == 0 or np.isnan(f["atr"][i]):
            i += 1
            continue
        px = c[i]
        stop = structural_stop(f, i, side, px)
        risk = (px - stop) * side
        if risk <= 0:
            i += 1
            continue
        tgt = px + side * rr * risk
        tp1 = px + side * tp1_r * risk
        got_tp1 = False
        pnl = 0.0
        j_end, reason = None, None
        for j in range(i + 1, min(n, i + 1 + max_bars)):
            hit_stop = l[j] <= stop if side > 0 else h[j] >= stop
            if hit_stop:
                ex = min(o[j], stop) if side > 0 else max(o[j], stop)
                frac = (1 - tp1_frac) if got_tp1 else 1.0
                pnl += frac * side * (ex / px - 1)
                j_end, reason = j, ("본전" if got_tp1 else "손절")
                break
            if mode == "bracket":
                if (h[j] >= tgt) if side > 0 else (l[j] <= tgt):
                    ex = max(o[j], tgt) if side > 0 else min(o[j], tgt)
                    pnl += side * (ex / px - 1)
                    j_end, reason = j, "목표"
                    break
            else:
                if not got_tp1 and ((h[j] >= tp1) if side > 0 else (l[j] <= tp1)):
                    ex = max(o[j], tp1) if side > 0 else min(o[j], tp1)
                    pnl += tp1_frac * side * (ex / px - 1)
                    got_tp1 = True
                    stop = px                                  # 본전 이동 (Pine과 같이 같은 봉 종가에 추적 조건도 확인)
                if got_tp1 and (c[j] - trail[j]) * side < 0:
                    pnl += (1 - tp1_frac) * side * (c[j] / px - 1)
                    j_end, reason = j, "추적 청산"
                    break
                if not got_tp1 and j - i >= pre_tp_bars:
                    pnl += side * (c[j] / px - 1)
                    j_end, reason = j, "시간"
                    break
        if j_end is None:
            j_end = min(n - 1, i + max_bars)
            frac = (1 - tp1_frac) if got_tp1 else 1.0
            pnl += frac * side * (c[j_end] / px - 1)
            reason = "시간"
        trades.append((i, j_end, side, px, risk / px, pnl, reason, 2 if (got_tp1 and mode == "manager") else 1))
        i = j_end + 1
    return pd.DataFrame(trades, columns=["i_in", "i_out", "side", "px", "stop_dist", "gross", "reason", "fills"])


def stats(t, fees=(0.0, 0.0002, 0.00075)):
    if not len(t):
        return {"trades": 0}
    g = t["gross"].to_numpy()
    legs = 2.0                                   # 진입 1회 + 청산 1회분 (분할 청산도 합치면 1회분)
    out = {"trades": int(len(t)), "win_gross": float((g > 0).mean()), "avg_gross_bp": float(g.mean() * 1e4),
           "avg_R": float(np.mean(g / t["stop_dist"].to_numpy())), "median_stop_pct": float(t["stop_dist"].median() * 100)}
    for fee in fees:
        net = g - legs * fee
        w, ls = net[net > 0], net[net <= 0]
        out[f"pf_{fee}"] = float(w.sum() / -ls.sum()) if ls.sum() < 0 else float("inf")
        out[f"exp_bp_{fee}"] = float(net.mean() * 1e4)
    return out
