"""무기한 선물 시세 다운로드 (BingX, Gate). 사례 검증과 알트코인 실험에 씁니다.

- BingX: /openApi/swap/v3/quote/klines, 1회 최대 1000봉, 1분봉도 과거 데이터 제공
- Gate : /api/v4/futures/usdt/candlesticks, 1회 최대 2000봉, 타임프레임별 최근 10,000봉까지만 제공
결과는 time(ms), open, high, low, close, volume 열의 DataFrame이고 CSV로 캐시합니다.
"""
import json
import os
import time
import urllib.request

import pandas as pd

MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}


def _get(url, tries=5):
    for i in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                return json.load(r)
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(1.5 * 2 ** i)


def bingx(sym, interval, start_ms, end_ms):
    rows = {}
    step = 1000 * MS[interval]
    t = start_ms
    while t < end_ms:
        e = min(t + step - 1, end_ms)
        url = (f"https://open-api.bingx.com/openApi/swap/v3/quote/klines?symbol={sym}-USDT"
               f"&interval={interval}&startTime={t}&endTime={e}&limit=1000")
        for i in range(6):
            d = _get(url)
            if d.get("code") == 0:
                break
            if "不存在" in str(d.get("msg")) or "not exist" in str(d.get("msg")):
                raise RuntimeError(f"BingX {sym}: {d.get('msg')}")
            time.sleep(2 * 2 ** i)                    # 호출 제한: 잠시 후 다시
        else:
            raise RuntimeError(f"BingX {sym}: {d.get('msg')}")
        for k in d.get("data") or []:
            rows[int(k["time"])] = (float(k["open"]), float(k["high"]), float(k["low"]), float(k["close"]), float(k["volume"]))
        t = e + 1
        time.sleep(0.25)
    return _frame(rows)


def gate(contract, interval, start_ms, end_ms):
    rows = {}
    step = 1999 * MS[interval]
    t = start_ms
    while t < end_ms:
        e = min(t + step, end_ms)
        url = (f"https://api.gateio.ws/api/v4/futures/usdt/candlesticks?contract={contract}_USDT"
               f"&interval={interval}&from={t // 1000}&to={e // 1000}")
        for k in _get(url):
            rows[int(k["t"]) * 1000] = (float(k["o"]), float(k["h"]), float(k["l"]), float(k["c"]), float(k["v"]))
        t = e + MS[interval]
        time.sleep(0.25)
    return _frame(rows)


def gate_contract(contract):
    return _get(f"https://api.gateio.ws/api/v4/futures/usdt/contracts/{contract}_USDT")


def _frame(rows):
    df = pd.DataFrame([(t, *v) for t, v in sorted(rows.items())],
                      columns=["time", "open", "high", "low", "close", "volume"])
    return df


def cached(path, fn, *args):
    if os.path.exists(path):
        return pd.read_csv(path)
    df = fn(*args)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    df.to_csv(path, index=False)
    return df
