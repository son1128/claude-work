"""저장된 시세로 PPP v2 신호 만들기.

L1(상위 시간봉 방향)은 진입 시간봉을 묶어 계산하지 않고, 수집기가 따로 받은 4시간봉으로 계산합니다.
Gate는 1분봉을 최근 1만 봉(약 7일)만 주므로, 1분봉을 묶어서는 4시간봉 EMA240(40일)을 만들 수 없습니다.
"""
import numpy as np

from ..core import features as fx
from ..core import oscillator as osc
from ..data.exchanges import INTERVAL_MS
from .models import Signal


def l1_series(tf_times, htf_df, htf, lens=(60, 120, 240)):
    """진입 시간봉의 각 봉에 대해 '직전에 마감된 상위 봉'의 EMA 배열 (+1/0/-1).
    Pine의 request.security(htf, align()[1], lookahead_on)와 같습니다. 상위 봉이 240개 미만이면 0."""
    out = np.zeros(len(tf_times), dtype=int)
    if htf_df is None or len(htf_df) < max(lens):
        return out
    c = htf_df["close"].to_numpy()
    e = [osc.pine_ema(c, n) for n in lens]
    align = np.where((e[0] > e[1]) & (e[1] > e[2]), 1, np.where((e[0] < e[1]) & (e[1] < e[2]), -1, 0))
    align[: max(lens) - 1] = 0
    step = INTERVAL_MS[htf]
    by_ts = dict(zip(htf_df["time"].to_numpy().tolist(), align.tolist()))
    for i, t in enumerate(tf_times):
        prev = (int(t) // step - 1) * step
        out[i] = by_ts.get(prev, 0)
    return out


def compute(df, htf_df, htf="4h"):
    l1 = l1_series(df["time"].to_numpy(), htf_df, htf)
    return fx.features(df, l1=l1)


def signal_at(df, f, i, symbol, tf, source="ppp-v2"):
    """i번째(마감된) 봉에서 골든/데드크로스가 났으면 Signal, 아니면 None."""
    o = f["osc"]
    if o["gc"][i]:
        side, score = 1, float(f["score_l"][i])
    elif o["dc"][i]:
        side, score = -1, float(f["score_s"][i])
    else:
        return None
    close = float(df["close"].iloc[i])
    if np.isnan(f["atr"][i]):
        return None
    stop = float(fx.structural_stop(f, i, side, close))
    ts_close = int(df["time"].iloc[i]) + INTERVAL_MS[tf]
    return Signal(symbol=symbol, side=side, price=close, ts_ms=ts_close, stop=stop, tf=tf, score=score,
                  l1=int(f["l1"][i]), source=source)


def structural_stop_now(df, htf_df, side, htf="4h"):
    """웹훅 신호에 손절가가 없을 때 쓸 구조적 손절가 (마지막 마감 봉 기준)."""
    f = compute(df, htf_df, htf)
    i = len(df) - 1
    return float(fx.structural_stop(f, i, side, float(df["close"].iloc[i]))), int(f["l1"][i]), f
