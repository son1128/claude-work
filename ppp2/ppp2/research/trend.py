"""단계 2 연구: 상위 시간봉 방향(L1) + 변동성 기준 비중 포트폴리오.

모든 계산은 인과적입니다. t봉 마감까지의 데이터로 정한 비중을 t+1봉 동안 보유합니다.

  direction()      방향 규칙: ema3 (EMA a<b<c 정·역배열), ema2 (빠른 EMA vs 느린 EMA 부호)
  target_weights() 자산별 위험 예산 = 목표 변동성 / 상장 종목 수 (상관 1 가정, 보수적)
                   + 선택: 포트폴리오 실현 변동성으로 전체 배율 조정 + 총 노출 상한
  apply_band()     리밸런싱 범위 (목표와 현재 비중 차이가 band 이하면 거래 안 함)
  simulate()       수수료·슬리피지·펀딩비를 뺀 봉 단위 수익
  walk_forward()   학습 구간 샤프로 설정 선택 → 다음 검증 구간에 적용, 검증 구간만 이어 붙임
  random_control() 자산별 방향 신호를 무작위로 시간 이동(빈도·롱숏 비율 보존) → 타이밍의 가치 확인
  block_bootstrap_sharpe() 일간 수익 블록 부트스트랩 신뢰구간
"""
from dataclasses import asdict, dataclass, replace

import numpy as np
import pandas as pd

BARS_PER_YEAR = {"4h": 6 * 365, "1d": 365}
DAY_MS = 86_400_000


@dataclass(frozen=True)
class TrendCfg:
    tf: str = "4h"
    rule: str = "ema3"                 # ema3 | ema2
    lens: tuple = (60, 120, 240)       # ema2는 앞의 두 값만 사용
    long_only: bool = False
    target_vol: float = 0.30           # 연 변동성 목표
    vol_days: int = 30                 # 자산 실현 변동성 창 (일)
    port_scale: bool = True            # 포트폴리오 실현 변동성으로 배율 조정
    port_days: int = 90
    scale_clip: tuple = (0.5, 2.5)
    asset_cap: float = 1.0             # 자산 하나의 비중 상한 (자본 대비)
    gross_cap: float = 3.0             # 총 노출 상한
    band: float = 0.2                  # 리밸런싱 범위 (상대)
    fee: float = 0.00075               # 편도 수수료 (Gate 시장가)
    slippage: float = 0.0002
    funding_apr: float = 0.10          # 롱이 내고 숏이 받는 연 펀딩비 가정

    def label(self):
        side = "롱만" if self.long_only else "롱·숏"
        lens = "/".join(str(x) for x in (self.lens if self.rule == "ema3" else self.lens[:2]))
        return f"{self.tf} {self.rule} {lens} {side}"


# ── 데이터 ──────────────────────────────────────────────────────────────────
def split_segments(df, step_ms, max_gap_bars=6):
    """하루 넘게 비는 구간이 있으면 앞뒤를 다른 종목으로 나눕니다.
    예: LUNA는 2022-05 붕괴 뒤 LUNA 2.0으로 재상장되어 가격이 17만 배 불연속, FTT는 10개월 공백 뒤 재개."""
    t = df["time"].to_numpy()
    cuts = np.flatnonzero(np.diff(t) > max_gap_bars * step_ms) + 1
    bounds = [0, *cuts.tolist(), len(df)]
    return [df.iloc[a:b] for a, b in zip(bounds, bounds[1:])]


def load_panel(store, exchange, symbols, base_tf="4h", min_bars=500):
    """종목별 종가를 시간 축으로 합친 DataFrame (index: ms). 상장 전·폐지 후는 NaN.
    중간에 하루 넘게 끊긴 종목은 'LUNA', 'LUNA~2'처럼 나눕니다."""
    from ..data.exchanges import INTERVAL_MS
    cols = {}
    for s in symbols:
        df = store.load(exchange, s, base_tf)
        for k, seg in enumerate(split_segments(df, INTERVAL_MS[base_tf])):
            if len(seg) >= min_bars:
                cols[s if k == 0 else f"{s}~{k + 1}"] = seg.set_index("time")["close"]
    close = pd.DataFrame(cols).sort_index()
    # 거래소 점검 등으로 잠깐 빈 봉은 직전 값으로 (최대 3봉), 상장 전·폐지 후는 그대로 NaN
    alive = close.notna().cumsum().gt(0) & close[::-1].notna().cumsum()[::-1].gt(0)
    return close.ffill(limit=3).where(alive)


def to_tf(close, tf):
    if tf == "4h":
        return close
    if tf == "1d":
        idx = pd.to_datetime(close.index, unit="ms")
        d = close.set_axis(idx).resample("1D", label="left", closed="left").last()
        d.index = (d.index - pd.Timestamp(0)) // pd.Timedelta(milliseconds=1)
        return d
    raise ValueError(tf)


# ── 신호 ────────────────────────────────────────────────────────────────────
def _ema(df, n):
    return df.ewm(span=n, adjust=False, ignore_na=True).mean().where(df.notna())


def direction(close, cfg):
    """+1 / 0 / -1. 종목마다 상장 후 가장 긴 EMA 길이만큼은 0 (워밍업)."""
    if cfg.rule == "ema3":
        a, b, c = (_ema(close, n) for n in cfg.lens)
        d = np.where((a > b) & (b > c), 1, np.where((a < b) & (b < c), -1, 0))
        warm = max(cfg.lens)
    elif cfg.rule == "ens3":
        # 길이 ×0.5, ×1, ×1.5 세 가지 ema3 방향의 평균 (-1 ~ +1). 설정 하나를 고르는 위험을 줄입니다
        ds = [direction(close, replace(cfg, rule="ema3", lens=tuple(max(2, int(round(x * m))) for x in cfg.lens)))
              for m in (0.5, 1.0, 1.5)]
        return sum(ds) / 3.0
    elif cfg.rule == "ema2":
        a, b = (_ema(close, n) for n in cfg.lens[:2])
        d = np.sign(a - b).fillna(0).to_numpy()
        warm = max(cfg.lens[:2])
    else:
        raise ValueError(cfg.rule)
    d = pd.DataFrame(d, index=close.index, columns=close.columns).astype(float)
    seen = close.notna().cumsum()
    d = d.where(seen > warm, 0.0).where(close.notna(), 0.0)
    if cfg.long_only:
        d = d.clip(lower=0)
    return d


def realized_vol(close, days, tf):
    bpy = BARS_PER_YEAR[tf]
    n = max(5, int(days * bpy / 365))
    r = np.log(close).diff()
    return r.rolling(n, min_periods=n // 2).std() * np.sqrt(bpy)


# ── 비중 ────────────────────────────────────────────────────────────────────
def target_weights(close, d, cfg):
    vol = realized_vol(close, cfg.vol_days, cfg.tf)
    alive = close.notna() & vol.notna()
    n_alive = alive.sum(axis=1).clip(lower=1)
    budget = cfg.target_vol / n_alive
    w = (d * vol.rdiv(1.0).mul(budget, axis=0)).clip(-cfg.asset_cap, cfg.asset_cap).where(alive, 0.0).fillna(0.0)
    if cfg.port_scale:
        r = close.pct_change(fill_method=None).fillna(0.0)
        base = (w.shift(1) * r).sum(axis=1)                       # 조정 전 비중의 과거 수익
        bpy = BARS_PER_YEAR[cfg.tf]
        n = max(10, int(cfg.port_days * bpy / 365))
        pv = base.rolling(n, min_periods=n // 2).std() * np.sqrt(bpy)
        scale = (cfg.target_vol / pv).clip(*cfg.scale_clip).fillna(1.0)
        w = w.mul(scale, axis=0)                                  # t까지의 수익으로 t의 비중 → 인과적
    gross = w.abs().sum(axis=1)
    over = (gross / cfg.gross_cap).clip(lower=1.0)
    return w.div(over, axis=0)


def apply_band(w, band):
    """목표 비중과 현재 비중 차이가 |현재| × band 이하면 그대로 둠. 방향 전환·진입·청산은 항상 실행."""
    if band <= 0:
        return w
    t = w.to_numpy()
    out = np.zeros_like(t)
    cur = np.zeros(t.shape[1])
    for i in range(len(t)):
        tgt = t[i]
        change = (np.sign(tgt) != np.sign(cur)) | (np.abs(tgt - cur) > band * np.abs(cur))
        cur = np.where(change, tgt, cur)
        out[i] = cur
    return pd.DataFrame(out, index=w.index, columns=w.columns)


# ── 시뮬레이션 ──────────────────────────────────────────────────────────────
def simulate(close, w, cfg):
    """봉 단위 순수익 Series와 구성 요소(DataFrame)."""
    r = close.pct_change(fill_method=None).fillna(0.0)
    w = w.where(close.notna(), 0.0).fillna(0.0)              # 폐지되면 비중 0 (청산 비용 발생)
    held = w.shift(1).fillna(0.0)
    gross_ret = (held * r).sum(axis=1)
    turnover = (w - held).abs().sum(axis=1)
    cost = turnover * (cfg.fee + cfg.slippage)
    fund_per_bar = cfg.funding_apr / BARS_PER_YEAR[cfg.tf]
    funding = held.sum(axis=1) * fund_per_bar                 # 순 롱 노출이 냄, 순 숏이 받음
    net = gross_ret - cost - funding
    parts = pd.DataFrame({"gross": gross_ret, "cost": cost, "funding": funding, "net": net, "turnover": turnover,
                          "gross_exposure": held.abs().sum(axis=1), "net_exposure": held.sum(axis=1),
                          "long_ret": (held.clip(lower=0) * r).sum(axis=1), "short_ret": (held.clip(upper=0) * r).sum(axis=1)})
    return net, parts


def run(close4h, cfg):
    close = to_tf(close4h, cfg.tf)
    d = direction(close, cfg)
    w = apply_band(target_weights(close, d, cfg), cfg.band)
    net, parts = simulate(close, w, cfg)
    return net, parts, w


# ── 통계 ────────────────────────────────────────────────────────────────────
def daily(net):
    idx = pd.to_datetime(net.index, unit="ms")
    return (1 + net.set_axis(idx)).resample("1D").prod() - 1


def stats(net, parts=None):
    dr = daily(net)
    dr = dr[dr.index >= dr.index[0]]
    if len(dr) < 30 or dr.std() == 0:
        return {"days": len(dr)}
    eq = (1 + dr).cumprod()
    years = len(dr) / 365
    cagr = eq.iloc[-1] ** (1 / years) - 1 if eq.iloc[-1] > 0 else -1.0
    vol = dr.std() * np.sqrt(365)
    sharpe = dr.mean() / dr.std() * np.sqrt(365)
    down = dr[dr < 0].std() * np.sqrt(365)
    mdd = (eq / eq.cummax() - 1).min()
    mon = (1 + dr).resample("ME").prod() - 1
    out = {"days": len(dr), "cagr": float(cagr), "vol": float(vol), "sharpe": float(sharpe),
           "sortino": float(dr.mean() * 365 / down) if down > 0 else None, "mdd": float(mdd),
           "calmar": float(cagr / -mdd) if mdd < 0 else None, "pos_months": float((mon > 0).mean()),
           "total": float(eq.iloc[-1] - 1)}
    if parts is not None:
        p = parts.loc[net.index]
        yrs = max((p.index[-1] - p.index[0]) / (365 * DAY_MS), 1e-9)
        out.update({"turnover_per_year": float(p["turnover"].sum() / yrs),
                    "avg_gross": float(p["gross_exposure"].mean()), "avg_net": float(p["net_exposure"].mean()),
                    "cost_per_year": float(p["cost"].sum() / yrs),
                    "funding_per_year": float(p["funding"].sum() / yrs),
                    "long_contrib": float(p["long_ret"].sum()), "short_contrib": float(p["short_ret"].sum())})
    return out


def by_year(net):
    dr = daily(net)
    return {int(y): float((1 + g).prod() - 1) for y, g in dr.groupby(dr.index.year)}


def block_bootstrap_sharpe(net, reps=2000, block=20, seed=7):
    """net: 봉 수익(index ms) 또는 이미 일간 수익(DatetimeIndex)."""
    dr = (net if isinstance(net.index, pd.DatetimeIndex) else daily(net)).to_numpy()
    n = len(dr)
    rng = np.random.default_rng(seed)
    out = np.empty(reps)
    for k in range(reps):
        starts = rng.integers(0, n - block, size=n // block + 1)
        s = np.concatenate([dr[a:a + block] for a in starts])[:n]
        out[k] = s.mean() / s.std() * np.sqrt(365) if s.std() > 0 else 0
    return {"p05": float(np.percentile(out, 5)), "p50": float(np.percentile(out, 50)), "p95": float(np.percentile(out, 95)),
            "prob_le_0": float((out <= 0).mean())}


# ── 기간 분할 검증 ──────────────────────────────────────────────────────────
def walk_forward(close4h, cfgs, train_days=730, test_days=182, start_ms=None):
    """모든 설정의 수익 Series를 한 번 계산(인과적)한 뒤, 창마다 학습 구간 샤프 1위를 다음 검증 구간에 씁니다."""
    series = {}
    for c in cfgs:
        net, _, _ = run(close4h, c)
        series[c] = daily(net)
    idx = next(iter(series.values())).index
    first = idx[0] if start_ms is None else pd.to_datetime(start_ms, unit="ms")
    t = first + pd.Timedelta(days=train_days)
    windows, oos = [], []
    while t + pd.Timedelta(days=30) <= idx[-1]:
        tr0, tr1, te1 = t - pd.Timedelta(days=train_days), t, t + pd.Timedelta(days=test_days)

        def sh(s):
            x = s[(s.index >= tr0) & (s.index < tr1)]
            return x.mean() / x.std() * np.sqrt(365) if len(x) > 60 and x.std() > 0 else -9
        best = max(cfgs, key=lambda c: sh(series[c]))
        test = series[best][(series[best].index >= tr1) & (series[best].index < te1)]
        oos.append(test)
        windows.append({"train_start": str(tr0.date()), "test_start": str(tr1.date()), "test_end": str(min(te1, idx[-1]).date()),
                        "chosen": best.label(), "train_sharpe": float(sh(series[best])),
                        "test_return": float((1 + test).prod() - 1),
                        "test_sharpe": float(test.mean() / test.std() * np.sqrt(365)) if test.std() > 0 else 0.0})
        t = te1
    oos = pd.concat(oos)
    return oos, windows, series


def daily_stats(dr):
    """일간 수익 Series 통계 (walk_forward 결과용)."""
    eq = (1 + dr).cumprod()
    years = len(dr) / 365
    mdd = (eq / eq.cummax() - 1).min()
    cagr = eq.iloc[-1] ** (1 / years) - 1 if eq.iloc[-1] > 0 else -1.0
    return {"days": len(dr), "cagr": float(cagr), "vol": float(dr.std() * np.sqrt(365)),
            "sharpe": float(dr.mean() / dr.std() * np.sqrt(365)), "mdd": float(mdd), "total": float(eq.iloc[-1] - 1),
            "pos_months": float(((1 + dr).resample("ME").prod() - 1 > 0).mean())}


def random_control(close4h, cfg, reps=200, seed=11):
    """자산별 방향 신호를 무작위 시간만큼 원형 이동. 전환 빈도·보유 기간·롱숏 비율은 그대로, 타이밍만 무작위."""
    close = to_tf(close4h, cfg.tf)
    d = direction(close, cfg)
    rng = np.random.default_rng(seed)
    out = []
    for _ in range(reps):
        dd = d.copy()
        for col in d.columns:
            life = close[col].notna().to_numpy()
            idx = np.flatnonzero(life)
            if len(idx) < 10:
                continue
            seg = d[col].to_numpy()[idx]
            k = rng.integers(len(seg) // 10, len(seg) - len(seg) // 10)
            arr = dd[col].to_numpy().copy()
            arr[idx] = np.roll(seg, k)
            dd[col] = arr
        w = apply_band(target_weights(close, dd, cfg), cfg.band)
        net, _ = simulate(close, w, cfg)
        dr = daily(net)
        out.append(dr.mean() / dr.std() * np.sqrt(365) if dr.std() > 0 else 0.0)
    return np.array(out)


def neighbors(cfg):
    """설정 안정성(plateau) 확인용 이웃 설정: EMA 길이·변동성 창·리밸런싱 범위·목표 변동성 ±25%."""
    out = []
    for m in (0.75, 1.25):
        out.append(("EMA 길이 ×%.2f" % m, replace(cfg, lens=tuple(max(2, int(round(x * m))) for x in cfg.lens))))
        out.append(("변동성 창 ×%.2f" % m, replace(cfg, vol_days=max(5, int(round(cfg.vol_days * m))))))
        out.append(("리밸런싱 범위 ×%.2f" % m, replace(cfg, band=cfg.band * m)))
        out.append(("목표 변동성 ×%.2f" % m, replace(cfg, target_vol=cfg.target_vol * m)))
    return out


def cfg_dict(c):
    return asdict(c)
