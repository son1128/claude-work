"""시세 저장소 (SQLite, WAL 모드).

수집기가 쓰고 엔진이 읽는 단일 파일 DB입니다. 같은 봉을 다시 받으면 덮어쓰므로(upsert)
중복 없이 여러 번 실행해도 됩니다.
"""
import json
import sqlite3
from dataclasses import asdict

import pandas as pd

from .exchanges import INTERVAL_MS, ContractSpec

SCHEMA = """
CREATE TABLE IF NOT EXISTS candles (
  exchange TEXT NOT NULL, symbol TEXT NOT NULL, interval TEXT NOT NULL, ts INTEGER NOT NULL,
  open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL, volume REAL NOT NULL,
  PRIMARY KEY (exchange, symbol, interval, ts)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS contracts (
  exchange TEXT NOT NULL, symbol TEXT NOT NULL, spec TEXT NOT NULL, updated_ms INTEGER NOT NULL,
  PRIMARY KEY (exchange, symbol)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS funding (
  exchange TEXT NOT NULL, symbol TEXT NOT NULL, ts INTEGER NOT NULL, rate REAL NOT NULL,
  PRIMARY KEY (exchange, symbol, ts)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS collector_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts_ms INTEGER NOT NULL, exchange TEXT, symbol TEXT, interval TEXT,
  action TEXT, inserted INTEGER, message TEXT);
"""


class Store:
    def __init__(self, path):
        self.path = path
        self.db = sqlite3.connect(path, timeout=30, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)

    def close(self):
        self.db.close()

    # ── 봉 ──────────────────────────────────────────────────────────────────
    def upsert_candles(self, exchange, symbol, interval, rows):
        """rows: [(ts, o, h, l, c, v)]. 새로 생긴 봉 수를 돌려줍니다."""
        if not rows:
            return 0
        before = self.count(exchange, symbol, interval)
        with self.db:
            self.db.executemany(
                "INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(exchange,symbol,interval,ts) DO UPDATE SET "
                "open=excluded.open, high=excluded.high, low=excluded.low, close=excluded.close, volume=excluded.volume",
                [(exchange, symbol, interval, *r) for r in rows])
        return self.count(exchange, symbol, interval) - before

    def count(self, exchange, symbol, interval):
        return self.db.execute("SELECT COUNT(*) FROM candles WHERE exchange=? AND symbol=? AND interval=?",
                               (exchange, symbol, interval)).fetchone()[0]

    def last_ts(self, exchange, symbol, interval):
        return self.db.execute("SELECT MAX(ts) FROM candles WHERE exchange=? AND symbol=? AND interval=?",
                               (exchange, symbol, interval)).fetchone()[0]

    def first_ts(self, exchange, symbol, interval):
        return self.db.execute("SELECT MIN(ts) FROM candles WHERE exchange=? AND symbol=? AND interval=?",
                               (exchange, symbol, interval)).fetchone()[0]

    def load(self, exchange, symbol, interval, start_ms=None, end_ms=None, limit=None):
        """시간순 DataFrame(time, open, high, low, close, volume). limit이면 가장 최근 limit개."""
        q = "SELECT ts AS time, open, high, low, close, volume FROM candles WHERE exchange=? AND symbol=? AND interval=?"
        args = [exchange, symbol, interval]
        if start_ms is not None:
            q += " AND ts>=?"
            args.append(start_ms)
        if end_ms is not None:
            q += " AND ts<=?"
            args.append(end_ms)
        if limit:
            q = f"SELECT * FROM ({q} ORDER BY ts DESC LIMIT {int(limit)}) ORDER BY time"
        else:
            q += " ORDER BY ts"
        return pd.read_sql_query(q, self.db, params=args)

    def series(self):
        return self.db.execute("SELECT exchange, symbol, interval, COUNT(*), MIN(ts), MAX(ts) FROM candles "
                               "GROUP BY exchange, symbol, interval ORDER BY exchange, symbol, interval").fetchall()

    def gaps(self, exchange, symbol, interval, start_ms=None, end_ms=None):
        """빠진 봉 구간 [(첫 빠진 ts, 마지막 빠진 ts, 개수)]."""
        step = INTERVAL_MS[interval]
        rows = self.db.execute(
            "SELECT ts FROM candles WHERE exchange=? AND symbol=? AND interval=? AND ts>=? AND ts<=? ORDER BY ts",
            (exchange, symbol, interval, start_ms or 0, end_ms or 2**62)).fetchall()
        out = []
        for (a,), (b,) in zip(rows, rows[1:]):
            if b - a > step:
                out.append((a + step, b - step, (b - a) // step - 1))
        return out

    def quality(self, exchange, symbol, interval):
        n, first, last = self.db.execute(
            "SELECT COUNT(*), MIN(ts), MAX(ts) FROM candles WHERE exchange=? AND symbol=? AND interval=?",
            (exchange, symbol, interval)).fetchone()
        if not n:
            return {"bars": 0}
        step = INTERVAL_MS[interval]
        bad = self.db.execute(
            "SELECT COUNT(*) FROM candles WHERE exchange=? AND symbol=? AND interval=? AND "
            "(high < MAX(open, close) OR low > MIN(open, close) OR low <= 0 OR high < low)",
            (exchange, symbol, interval)).fetchone()[0]
        flat = self.db.execute(
            "SELECT COUNT(*) FROM candles WHERE exchange=? AND symbol=? AND interval=? AND volume=0 AND high=low",
            (exchange, symbol, interval)).fetchone()[0]
        g = self.gaps(exchange, symbol, interval)
        expected = (last - first) // step + 1
        return {"bars": n, "first": first, "last": last, "expected": expected, "missing": expected - n,
                "missing_pct": (expected - n) / expected * 100, "gap_ranges": len(g), "bad_ohlc": bad, "flat_zero_volume": flat}

    # ── 계약 정보·펀딩 ──────────────────────────────────────────────────────
    def upsert_contracts(self, specs, now_ms):
        with self.db:
            self.db.executemany("INSERT OR REPLACE INTO contracts VALUES (?,?,?,?)",
                                [(s.exchange, s.symbol, json.dumps(asdict(s)), now_ms) for s in specs])
        return len(specs)

    def contract(self, exchange, symbol):
        r = self.db.execute("SELECT spec FROM contracts WHERE exchange=? AND symbol=?", (exchange, symbol)).fetchone()
        return ContractSpec(**json.loads(r[0])) if r else None

    def upsert_funding(self, exchange, symbol, rows):
        with self.db:
            self.db.executemany("INSERT OR REPLACE INTO funding VALUES (?,?,?,?)", [(exchange, symbol, t, r) for t, r in rows])
        return len(rows)

    def funding(self, exchange, symbol):
        return self.db.execute("SELECT ts, rate FROM funding WHERE exchange=? AND symbol=? ORDER BY ts",
                               (exchange, symbol)).fetchall()

    def log(self, ts_ms, exchange, symbol, interval, action, inserted=0, message=""):
        with self.db:
            self.db.execute("INSERT INTO collector_log (ts_ms, exchange, symbol, interval, action, inserted, message) "
                            "VALUES (?,?,?,?,?,?,?)", (ts_ms, exchange, symbol, interval, action, inserted, message))
