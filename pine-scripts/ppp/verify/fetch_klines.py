"""바이낸스 공개 시세(data-api.binance.vision)에서 1분봉을 내려받습니다.

사용법: python fetch_klines.py OUT_DIR DAYS SYMBOL1,SYMBOL2,...
결과: OUT_DIR/<SYMBOL>_1m.csv (time, open, high, low, close, volume)
"""
import json, sys, time, urllib.request, concurrent.futures as cf, csv, os
OUT = sys.argv[1]; DAYS = int(sys.argv[2]); SYMS = sys.argv[3].split(",")
BASE = "https://data-api.binance.vision/api/v3/klines"
end = int(time.time()//60*60*1000); start = end - DAYS*86400*1000
def get(sym, t0):
    url = f"{BASE}?symbol={sym}&interval=1m&startTime={t0}&limit=1000"
    for i in range(5):
        try:
            with urllib.request.urlopen(url, timeout=30) as r: return json.load(r)
        except Exception: time.sleep(2**i)
    raise RuntimeError(url)
for sym in SYMS:
    starts = list(range(start, end, 1000*60000))
    with cf.ThreadPoolExecutor(8) as ex: chunks = list(ex.map(lambda t: get(sym, t), starts))
    rows = {}
    for ch in chunks:
        for k in ch: rows[k[0]] = k[:6]
    with open(os.path.join(OUT, f"{sym}_1m.csv"), "w", newline="") as f:
        w = csv.writer(f); w.writerow(["time","open","high","low","close","volume"])
        for t in sorted(rows): w.writerow(rows[t])
    print(sym, len(rows), flush=True)
