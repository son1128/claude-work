"""엔진 기록 (SQLite). 신호, 위험 점검 결과(통과·거부 이유), 포지션, 체결·이벤트, 자산 기록, 상태값."""
import json
import sqlite3
from dataclasses import asdict

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts_ms INTEGER, mode TEXT, symbol TEXT, side INTEGER, price REAL, stop REAL,
  tf TEXT, score REAL, l1 INTEGER, source TEXT);
CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, signal_id INTEGER, ts_ms INTEGER, mode TEXT, approved INTEGER,
  summary TEXT, checks TEXT, plan TEXT);
CREATE TABLE IF NOT EXISTS positions (
  mode TEXT, id INTEGER, symbol TEXT, side INTEGER, status TEXT, data TEXT, PRIMARY KEY (mode, id));
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts_ms INTEGER, mode TEXT, type TEXT, position_id INTEGER, symbol TEXT, data TEXT);
CREATE TABLE IF NOT EXISTS equity (
  ts_ms INTEGER, mode TEXT, equity REAL, free REAL, PRIMARY KEY (mode, ts_ms));
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT);
"""


class Journal:
    def __init__(self, path, mode):
        self.mode = mode
        self.db = sqlite3.connect(path, timeout=30, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    def close(self):
        self.db.close()

    def signal(self, sig):
        with self.db:
            cur = self.db.execute(
                "INSERT INTO signals (ts_ms, mode, symbol, side, price, stop, tf, score, l1, source) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (sig.ts_ms, self.mode, sig.symbol, sig.side, sig.price, sig.stop, sig.tf, sig.score, sig.l1, sig.source))
        return cur.lastrowid

    def decision(self, signal_id, ts_ms, d):
        with self.db:
            self.db.execute(
                "INSERT INTO decisions (signal_id, ts_ms, mode, approved, summary, checks, plan) VALUES (?,?,?,?,?,?,?)",
                (signal_id, ts_ms, self.mode, int(d.approved), d.summary(),
                 json.dumps([asdict(c) for c in d.checks], ensure_ascii=False),
                 json.dumps(d.plan.to_dict() if d.plan else None)))

    def position(self, pos):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO positions VALUES (?,?,?,?,?,?)",
                            (self.mode, pos.id, pos.symbol, pos.side, pos.status, json.dumps(asdict(pos), ensure_ascii=False)))

    def events(self, evs):
        if not evs:
            return
        with self.db:
            self.db.executemany("INSERT INTO events (ts_ms, mode, type, position_id, symbol, data) VALUES (?,?,?,?,?,?)",
                                [(e["ts_ms"], self.mode, e["type"], e.get("position_id"), e.get("symbol"),
                                  json.dumps(e, ensure_ascii=False)) for e in evs])

    def equity_point(self, ts_ms, equity, free):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO equity VALUES (?,?,?,?)", (ts_ms, self.mode, equity, free))

    def equity_at_or_after(self, ts_ms):
        r = self.db.execute("SELECT equity FROM equity WHERE mode=? AND ts_ms>=? ORDER BY ts_ms LIMIT 1",
                            (self.mode, ts_ms)).fetchone()
        return r[0] if r else None

    def get(self, key, default=None):
        r = self.db.execute("SELECT value FROM state WHERE key=?", (f"{self.mode}:{key}",)).fetchone()
        return json.loads(r[0]) if r else default

    def set(self, key, value):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO state VALUES (?,?)", (f"{self.mode}:{key}", json.dumps(value, ensure_ascii=False)))

    def recent_decisions(self, n=20):
        return self.db.execute("SELECT ts_ms, approved, summary FROM decisions WHERE mode=? ORDER BY id DESC LIMIT ?",
                               (self.mode, n)).fetchall()

    def closed_positions(self):
        return [json.loads(r[0]) for r in self.db.execute(
            "SELECT data FROM positions WHERE mode=? AND status='closed' ORDER BY id", (self.mode,)).fetchall()]
