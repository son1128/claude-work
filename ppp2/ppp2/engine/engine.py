"""실행 엔진: 신호 → 위험 점검 → 주문 → 청산 관리 → 기록.

  submit(signal)     웹훅·수동·자동 신호를 위험 점검에 통과시키고 승인되면 주문
  step()             실시간 루프 한 번: 새로 마감된 봉마다 체결 처리 → 청산 관리 → 자동 신호
  replay(...)        저장된 과거 시세로 같은 경로를 처음부터 끝까지 돌려 보는 모의 실행
  halt() / resume()  신규 진입 정지·재개 (data/HALT 파일로도 정지 가능). 보유 포지션 관리는 계속합니다.
"""
import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone

import numpy as np

from ..core import oscillator as osc
from ..data.exchanges import INTERVAL_MS
from . import signals as sg
from .manager import ExitManager
from .risk import AccountContext, RiskEngine

log = logging.getLogger("ppp2.engine")
WARMUP = 300


class Engine:
    def __init__(self, cfg, store, broker, journal, now_ms=None):
        self.cfg = cfg
        self.store = store
        self.broker = broker
        self.journal = journal
        self.risk = RiskEngine(cfg.risk, cfg.exits)
        self.mgr = ExitManager(cfg.exits)
        self.ex = cfg.engine.exchange
        self.tf = cfg.engine.tf
        self.htf = cfg.engine.l1_tf
        self._now = now_ms or (lambda: int(time.time() * 1000))
        self._clock = None                      # replay 중에는 봉 마감 시각
        self.last_px = {}
        self.lock = threading.RLock()
        self.halt_file = os.path.join(cfg.general.state_dir, "HALT")

    def now(self):
        return self._clock if self._clock is not None else self._now()

    # ── 정지·재개 ───────────────────────────────────────────────────────────
    def halted(self):
        if os.path.exists(self.halt_file):
            with open(self.halt_file, encoding="utf-8") as fh:
                return True, "HALT 파일: " + (fh.read().strip() or "사유 없음")
        h = self.journal.get("halt")
        return (True, h) if h else (False, "")

    def halt(self, reason):
        self.journal.set("halt", reason)
        self.journal.events([{"type": "halt", "ts_ms": self.now(), "reason": reason}])
        log.warning("신규 진입 정지: %s", reason)

    def resume(self):
        self.journal.set("halt", None)
        self.journal.events([{"type": "resume", "ts_ms": self.now()}])

    # ── 계좌 상태 ───────────────────────────────────────────────────────────
    def _period_start(self, now_ms, weekly):
        tz = timezone(timedelta(hours=self.cfg.general.tz_offset_hours))
        d = datetime.fromtimestamp(now_ms / 1000, tz).replace(hour=0, minute=0, second=0, microsecond=0)
        if weekly:
            d -= timedelta(days=d.weekday())
        return int(d.timestamp() * 1000)

    def snapshot(self):
        eq = self.broker.equity(self.last_px)
        self.journal.equity_point(self.now(), eq, self.broker.free(self.last_px))
        return eq

    def context(self, symbol, price, data_age_s):
        now = self.now()
        eq = self.broker.equity(self.last_px)
        day = self.journal.equity_at_or_after(self._period_start(now, False)) or eq
        week = self.journal.equity_at_or_after(self._period_start(now, True)) or eq
        h, why = self.halted()
        return AccountContext(equity=eq, free=self.broker.free(self.last_px), now_ms=now, price=price,
                              spec=self.store.contract(self.ex, symbol), open_positions=self.broker.open_positions(),
                              data_age_s=data_age_s, day_start_equity=day, week_start_equity=week, halted=h, halt_reason=why)

    # ── 신호 처리 ───────────────────────────────────────────────────────────
    def _market(self, symbol):
        """현재가와 시세 경과 시간. 1분봉이 있으면 1분봉, 없으면 진입 시간봉의 마지막 종가."""
        for iv in ("1m", self.tf):
            d = self.store.load(self.ex, symbol, iv, limit=1)
            if len(d):
                age = (self.now() - (int(d["time"].iloc[-1]) + INTERVAL_MS[iv])) / 1000
                return float(d["close"].iloc[-1]), max(age, 0.0)
        return 0.0, float("inf")

    def submit(self, sig, price=None, data_age_s=None, bar_ts=None):
        with self.lock:
            if price is None:
                price, data_age_s = self._market(sig.symbol)
            if sig.source != "ppp-v2":
                # 외부 신호(웹훅·수동)의 L1은 믿지 않고 엔진이 가진 4시간봉으로 다시 계산합니다.
                # 손절가가 없으면 구조적 손절가를 채웁니다.
                df = self.store.load(self.ex, sig.symbol, self.tf, limit=self.cfg.engine.history_bars)
                hdf = self.store.load(self.ex, sig.symbol, self.htf, limit=600)
                if len(df) >= WARMUP:
                    stop, l1, _ = sg.structural_stop_now(df, hdf, sig.side, self.htf)
                    if sig.l1 is not None and sig.l1 != l1:
                        sig.source += f" (알림 L1 {sig.l1} → 엔진 L1 {l1})"
                    sig.l1 = l1
                    sig.stop = stop if sig.stop is None else sig.stop
                    bar_ts = bar_ts or int(df["time"].iloc[-1])
            self.last_px[sig.symbol] = price
            sid = self.journal.signal(sig)
            d = self.risk.check(sig, self.context(sig.symbol, price, data_age_s or 0.0))
            self.journal.decision(sid, self.now(), d)
            log.info(d.summary())
            if not d.approved:
                return d
            spec = self.store.contract(self.ex, sig.symbol)
            try:
                pos = self.broker.open(d.plan, self.now(), self.tf, bar_ts or self.now(), spec.contract_size)
            except Exception as e:                       # 주문 실패는 즉시 정지: 사람이 확인해야 함
                self.journal.events([{"type": "order_error", "ts_ms": self.now(), "symbol": sig.symbol, "error": str(e)}])
                self.halt(f"주문 오류 {sig.symbol}: {e}")
                raise
            self.journal.position(pos)
            self._flush()
            return d

    def _flush(self):
        evs = self.broker.drain_events()
        self.journal.events(evs)
        touched = {e.get("position_id") for e in evs}
        for p in list(self.broker.open_positions()) + list(getattr(self.broker, "closed", [])):
            if p.id in touched:
                self.journal.position(p)

    # ── 봉 처리 ─────────────────────────────────────────────────────────────
    def process_bar(self, symbol, df, f, trail, i, auto=True, live=False):
        ts = int(df["time"].iloc[i])
        o, h, l, c = (float(df[k].iloc[i]) for k in ("open", "high", "low", "close"))
        close_ms = ts + INTERVAL_MS[self.tf]
        if not live:
            self._clock = close_ms
        self.last_px[symbol] = c
        if hasattr(self.broker, "on_bar"):
            self.broker.on_bar(symbol, ts, o, h, l, c)
        for pos in [p for p in self.broker.open_positions() if p.symbol == symbol and p.entry_bar_ts < ts]:
            self.mgr.on_bar_close(pos, (ts, o, h, l, c), float(trail[i]), self.broker, close_ms)
        self._flush()
        eq = self.snapshot()
        # 일일 손실 한도는 위험 점검(DAILY_LOSS)이 그날 안에서만 막습니다. 다음 날 자동으로 풀리므로
        # 수동 정지(halt)와 섞지 않고, 처음 도달한 날 한 번 기록만 남깁니다.
        start = self._period_start(self.now(), False)
        day = self.journal.equity_at_or_after(start) or eq
        if (day - eq) / day * 100 >= self.cfg.risk.daily_loss_pct and self.journal.get("daily_limit_day") != start:
            self.journal.set("daily_limit_day", start)
            self.journal.events([{"type": "daily_loss_limit", "ts_ms": self.now(), "equity": eq, "day_start": day}])
            log.warning("일일 손실 한도 %.1f%% 도달: 오늘은 신규 진입 없음", self.cfg.risk.daily_loss_pct)
        if auto and self.cfg.engine.auto_signals:
            sig = sg.signal_at(df, f, i, symbol, self.tf)
            if sig is not None:
                if live:
                    self.submit(sig, bar_ts=ts)
                else:
                    self.submit(sig, price=c, data_age_s=0.0, bar_ts=ts)

    def step(self):
        """실시간 한 바퀴. 처음 실행하면 과거 신호를 다시 내지 않도록 마지막 봉부터 시작합니다."""
        if hasattr(self.broker, "sync"):
            self.broker.sync(self.now())
            self._flush()
        done = 0
        for sym in self.cfg.engine.symbols:
            df = self.store.load(self.ex, sym, self.tf, limit=self.cfg.engine.history_bars)
            if len(df) < WARMUP:
                log.warning("%s %s 봉이 %d개뿐이라 건너뜀 (최소 %d)", sym, self.tf, len(df), WARMUP)
                continue
            hdf = self.store.load(self.ex, sym, self.htf, limit=600)
            key = f"last_bar:{sym}:{self.tf}"
            last = self.journal.get(key)
            times = df["time"].to_numpy()
            if last is None:
                last = int(times[-2])
            new = np.flatnonzero(times > last)
            if not len(new):
                continue
            f = sg.compute(df, hdf, self.htf)
            trail = osc.pine_ema(df["close"].to_numpy(), self.cfg.exits.trail_len)
            for i in new:
                self.process_bar(sym, df, f, trail, int(i), live=True)
                self.journal.set(key, int(times[i]))
                done += 1
        if hasattr(self.broker, "save_state"):
            self.journal.set("broker_state", self.broker.save_state())
        return done

    def run_forever(self, should_stop=lambda: False):
        while not should_stop():
            try:
                self.step()
            except Exception:
                log.exception("엔진 루프 오류")
            time.sleep(self.cfg.engine.poll_seconds)

    # ── 과거 시세로 모의 실행 ─────────────────────────────────────────────────
    def replay(self, symbols=None, start_ms=None, end_ms=None):
        """봉을 하나씩 진행시키며 실시간과 같은 경로(체결 → 청산 관리 → 신호 → 위험 점검 → 주문)를 실행합니다."""
        streams = []
        for sym in symbols or self.cfg.engine.symbols:
            df = self.store.load(self.ex, sym, self.tf, end_ms=end_ms)
            hdf = self.store.load(self.ex, sym, self.htf, end_ms=end_ms)
            if len(df) < WARMUP:
                continue
            f = sg.compute(df, hdf, self.htf)
            trail = osc.pine_ema(df["close"].to_numpy(), self.cfg.exits.trail_len)
            first = WARMUP if start_ms is None else max(WARMUP, int(np.searchsorted(df["time"].to_numpy(), start_ms)))
            streams.append((sym, df, f, trail, first))
        # 여러 종목을 시간순으로 섞어 진행 (계좌 한도가 종목 간에 공유되므로)
        order = sorted((int(df["time"].iloc[i]), k, i) for k, (_, df, _, _, first) in enumerate(streams)
                       for i in range(first, len(df)))
        for _, k, i in order:
            sym, df, f, trail, _ = streams[k]
            self.process_bar(sym, df, f, trail, i)
        self._clock = None
        return self.summary()

    def summary(self):
        closed = getattr(self.broker, "closed", [])
        pnl = [p.realized - p.fees for p in closed]
        rows = self.journal.db.execute("SELECT approved, checks FROM decisions WHERE mode=?", (self.journal.mode,)).fetchall()
        reasons = {}
        for ok, ch in rows:
            if not ok:
                for c in json.loads(ch):
                    if not c["ok"]:
                        reasons[c["code"]] = reasons.get(c["code"], 0) + 1
        eq = [r[0] for r in self.journal.db.execute("SELECT equity FROM equity WHERE mode=? ORDER BY ts_ms", (self.journal.mode,))]
        peak, mdd = -1e18, 0.0
        for e in eq:
            peak = max(peak, e)
            mdd = min(mdd, e / peak - 1)
        exits = {}
        for p in closed:
            exits[p.exit_reason] = exits.get(p.exit_reason, 0) + 1
        return {"signals": len(rows), "approved": sum(r[0] for r in rows), "rejected_by": reasons,
                "trades": len(closed), "open": len(self.broker.open_positions()),
                "win_rate": (sum(x > 0 for x in pnl) / len(pnl)) if pnl else None,
                "net_pnl": sum(pnl), "equity": self.broker.equity(self.last_px), "max_drawdown": mdd,
                "exit_reasons": exits, "liquidations": exits.get("강제 청산", 0)}
