"""PPP 재현 지표와 4P 전략의 Python 구현.

ppp-oscillator.pine / ppp-4p-strategy.pine 과 같은 계산을 Pine의 내장 함수
정의(ta.rma, ta.ema, ta.rsi, ta.crossover, ta.barssince …)를 그대로 따라 옮겼습니다.

원본: pine-scripts/ppp/verify/ppp_core.py (tests/test_parity.py가 두 구현의 결과가 같은지 확인합니다)
"""
import numpy as np
import pandas as pd


# ─── Pine 내장 함수 ───────────────────────────────────────────────────────────
def pine_ema(src, length):
    """ta.ema: 첫 유효값에서 시작, alpha = 2 / (length + 1)."""
    a = 2.0 / (length + 1)
    out = np.full(len(src), np.nan)
    prev = np.nan
    for i, x in enumerate(src):
        if np.isnan(x):
            out[i] = prev if not np.isnan(prev) else np.nan
            continue
        prev = x if np.isnan(prev) else a * x + (1 - a) * prev
        out[i] = prev
    return out


def pine_rma(src, length):
    """ta.rma: 처음 length개 SMA로 시작, alpha = 1 / length."""
    a = 1.0 / length
    out = np.full(len(src), np.nan)
    prev = np.nan
    buf = []
    for i, x in enumerate(src):
        if np.isnan(x):
            continue
        if np.isnan(prev):
            buf.append(x)
            if len(buf) == length:
                prev = sum(buf) / length
                out[i] = prev
            continue
        prev = a * x + (1 - a) * prev
        out[i] = prev
    return out


def pine_sma(src, length):
    return pd.Series(src).rolling(length).mean().to_numpy()


def pine_rsi(close, length):
    ch = np.diff(close, prepend=np.nan)
    up = pine_rma(np.where(np.isnan(ch), np.nan, np.maximum(ch, 0)), length)
    dn = pine_rma(np.where(np.isnan(ch), np.nan, np.maximum(-ch, 0)), length)
    with np.errstate(divide="ignore", invalid="ignore"):
        rsi = np.where(dn == 0, 100.0, np.where(up == 0, 0.0, 100 - 100 / (1 + up / dn)))
    rsi[np.isnan(up) | np.isnan(dn)] = np.nan
    return rsi


def crossover(a, b):
    a1, b1 = np.roll(a, 1), np.roll(b, 1)
    r = (a > b) & (a1 <= b1)
    r[0] = False
    return r


def barssince_lt(cond, win):
    """ta.barssince(cond) < win (한 번도 없으면 False)."""
    out = np.zeros(len(cond), dtype=bool)
    last = -10**9
    for i, c in enumerate(cond):
        if c:
            last = i
        out[i] = (i - last) < win
    return out


# ─── 오실레이터 ───────────────────────────────────────────────────────────────
def ppp_oscillator(close, rsi_len=14, stoch_len=14, k_len=3, d_len=3, smooth="EMA",
                   ob=75.0, os_=25.0):
    rsi = pine_rsi(close, rsi_len)
    s = pd.Series(rsi)
    hh = s.rolling(stoch_len).max().to_numpy()
    ll = s.rolling(stoch_len).min().to_numpy()
    st = np.empty(len(close))
    prev = np.nan
    for i in range(len(close)):
        rng = hh[i] - ll[i]
        if not np.isnan(rng) and rng > 0:
            prev = 100 * (rsi[i] - ll[i]) / rng
        else:
            prev = 50.0 if np.isnan(prev) else prev
        st[i] = prev
    f = pine_ema if smooth == "EMA" else pine_sma
    k = f(st, k_len)
    d = f(k, d_len)
    gap = k - d
    gap1 = np.nan_to_num(np.roll(gap, 1))
    gap1[0] = 0
    widening = np.abs(gap) > np.abs(gap1)
    state = np.where(gap >= 0, np.where(widening, 1, 2), np.where(widening, 3, 4))
    gc = crossover(k, d)
    dc = crossover(d, k)
    lvl = (k + d) / 2
    return dict(rsi=rsi, stoch=st, k=k, d=d, state=state, gc=gc, dc=dc, lvl=lvl,
                gc_strong=gc & (lvl <= os_), dc_strong=dc & (lvl >= ob))


# ─── 4P 신호 ──────────────────────────────────────────────────────────────────
def htf_direction(df, htf="60min", lens=(60, 120, 240)):
    """request.security(htf, htfAlign()[1], lookahead_on): 직전 확정 상위 봉의 EMA 배열."""
    t = pd.to_datetime(df["time"], unit="ms")
    h = df.set_index(t)["close"].resample(htf, label="left", closed="left").last().dropna()
    e = [pine_ema(h.to_numpy(), n) for n in lens]
    dirn = np.where((e[0] > e[1]) & (e[1] > e[2]), 1, np.where((e[0] < e[1]) & (e[1] < e[2]), -1, 0))
    dirs = pd.Series(dirn, index=h.index).shift(1)          # [1]: 직전 상위 봉
    key = t.dt.floor(pd.Timedelta(htf))
    return dirs.reindex(key).fillna(0).to_numpy().astype(int)


def four_p_signals(df, osc, use1p=True, use2p=True, lens=(60, 120, 240), touch_win=5,
                   zone_only=False, htf="60min"):
    c, hi, lo = df["close"].to_numpy(), df["high"].to_numpy(), df["low"].to_numpy()
    emas = [pine_ema(c, n) for n in lens]
    touched = [barssince_lt((lo <= e) & (hi >= e), touch_win) for e in emas]
    zl = np.zeros(len(c), bool)
    zs = np.zeros(len(c), bool)
    for t, e in zip(touched, emas):
        zl |= t & (c > e)
        zs |= t & (c < e)
    hdir = htf_direction(df, htf, lens) if use1p else np.zeros(len(c), int)
    dir_l = (hdir == 1) if use1p else np.ones(len(c), bool)
    dir_s = (hdir == -1) if use1p else np.ones(len(c), bool)
    zone_ok_l = zl if use2p else np.ones(len(c), bool)
    zone_ok_s = zs if use2p else np.ones(len(c), bool)
    sig_l = osc["gc_strong"] | ((not zone_only) & osc["gc"] & use2p & zl)
    sig_s = osc["dc_strong"] | ((not zone_only) & osc["dc"] & use2p & zs)
    return dict(long=sig_l & dir_l & zone_ok_l, short=sig_s & dir_s & zone_ok_s,
                emas=emas, touched=touched, hdir=hdir)


# ─── 백테스트 (ppp-4p-strategy.pine 규칙) ────────────────────────────────────
def backtest(df, osc, sig, fee=0.0005, bb_len=20, bb_mult=2.0, use_tp=True, use_opp=True,
             use_stop=True, stop_buf=0.0005, allow_long=True, allow_short=True):
    o, h, l, c = (df[x].to_numpy() for x in ("open", "high", "low", "close"))
    s = pd.Series(c)
    basis = s.rolling(bb_len).mean().to_numpy()
    dev = bb_mult * s.rolling(bb_len).std(ddof=0).to_numpy()   # ta.stdev = 모표준편차
    bb_up, bb_dn = basis + dev, basis - dev
    emas, touched = sig["emas"], sig["touched"]
    gc, dc = osc["gc"], osc["dc"]
    trades = []
    pos, entry, entry_i, stop_idx, tp = 0, 0.0, 0, 0, np.nan
    for i in range(len(c)):
        # 1) 직전 봉에서 걸어 둔 BB 지정가가 이번 봉 안에서 체결되는가
        if pos != 0 and use_tp and not np.isnan(tp):
            if pos > 0 and h[i] >= tp:
                trades.append((entry_i, i, 1, entry, max(o[i], tp), "BB 익절"))
                pos = 0
            elif pos < 0 and l[i] <= tp:
                trades.append((entry_i, i, -1, entry, min(o[i], tp), "BB 익절"))
                pos = 0
        # 2) 봉 마감 처리 (process_orders_on_close)
        closed_now = False
        if pos > 0:
            se = emas[stop_idx][i]
            if (use_opp and dc[i]) or (use_stop and c[i] < se * (1 - stop_buf)):
                trades.append((entry_i, i, 1, entry, c[i], "반대 크로스" if dc[i] else "EMA 이탈"))
                pos, closed_now = 0, True
        elif pos < 0:
            se = emas[stop_idx][i]
            if (use_opp and gc[i]) or (use_stop and c[i] > se * (1 + stop_buf)):
                trades.append((entry_i, i, -1, entry, c[i], "반대 크로스" if gc[i] else "EMA 돌파"))
                pos, closed_now = 0, True
        if pos == 0 and not closed_now:   # Pine: 같은 실행에서 strategy.close 후에는 position_size가 아직 0이 아님
            if allow_long and sig["long"][i]:
                pos, entry, entry_i = 1, c[i], i
                cands = [(emas[j][i], j) for j in range(3) if touched[j][i] and c[i] > emas[j][i]]
                stop_idx = max(cands)[1] if cands else 0
            elif allow_short and sig["short"][i]:
                pos, entry, entry_i = -1, c[i], i
                cands = [(emas[j][i], j) for j in range(3) if touched[j][i] and c[i] < emas[j][i]]
                stop_idx = min(cands)[1] if cands else 0
        tp = (bb_up[i] if pos > 0 else bb_dn[i]) if pos != 0 else np.nan
    t = pd.DataFrame(trades, columns=["i_in", "i_out", "side", "px_in", "px_out", "reason"])
    if len(t):
        t["gross"] = t["side"] * (t["px_out"] / t["px_in"] - 1)
        t["net"] = t["gross"] - 2 * fee
        t["bars"] = t["i_out"] - t["i_in"]
    return t


def simple_cross_backtest(osc, close, fee=0.0005, zone=False):
    """필터 없이 크로스만으로 롱/숏 전환 (지표 단독 성능)."""
    gl = osc["gc_strong"] if zone else osc["gc"]
    gs = osc["dc_strong"] if zone else osc["dc"]
    trades, pos, px, ii = [], 0, 0.0, 0
    for i in range(len(close)):
        if pos > 0 and osc["dc"][i]:
            trades.append((ii, i, 1, px, close[i], "반대 크로스")); pos = 0
        elif pos < 0 and osc["gc"][i]:
            trades.append((ii, i, -1, px, close[i], "반대 크로스")); pos = 0
        if pos == 0:
            if gl[i]:
                pos, px, ii = 1, close[i], i
            elif gs[i]:
                pos, px, ii = -1, close[i], i
    t = pd.DataFrame(trades, columns=["i_in", "i_out", "side", "px_in", "px_out", "reason"])
    if len(t):
        t["gross"] = t["side"] * (t["px_out"] / t["px_in"] - 1)
        t["net"] = t["gross"] - 2 * fee
        t["bars"] = t["i_out"] - t["i_in"]
    return t


def summarize(t, days):
    if not len(t):
        return dict(trades=0)
    net = t["net"].to_numpy()
    eq = np.cumprod(1 + net)
    peak = np.maximum.accumulate(eq)
    wins, losses = net[net > 0], net[net <= 0]
    return dict(
        trades=len(t),
        per_day=len(t) / days,
        win_gross=float((t["gross"] > 0).mean()),
        win_net=float((net > 0).mean()),
        avg_win=float(wins.mean()) if len(wins) else 0.0,
        avg_loss=float(losses.mean()) if len(losses) else 0.0,
        pf=float(wins.sum() / -losses.sum()) if len(losses) and losses.sum() < 0 else float("inf"),
        total_gross=float(np.prod(1 + t["gross"].to_numpy()) - 1),
        total_net=float(eq[-1] - 1),
        mdd=float((eq / peak - 1).min()),
        avg_bars=float(t["bars"].mean()),
    )
