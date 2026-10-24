"""명령줄 도구.  python -m ppp2 --help

수집기
  collect contracts                  계약 정보(유지 증거금률·최대 레버리지·수수료·주문 단위) 저장
  collect update   [-s BTC,ETH] [-i 1m,15m,4h]   마지막 봉 이후 이어 받기 (처음이면 lookback일)
  collect backfill --days N          저장된 첫 봉 앞쪽 채우기
  collect repair                     빠진 봉 다시 받기
  collect check                      품질 점검 (빠진 봉, 이상한 OHLC)
  collect universe --top N           24시간 거래대금 상위 종목
  collect run                        주기 실행 (Ctrl+C로 종료)
엔진
  engine risk   -s BTC --side long [--price P] [--stop S] [--l1 1]   위험 점검만 (주문 안 함)
  engine submit -s BTC --side long ...                              위험 점검 후 승인되면 주문
  engine run    [--webhook]          실시간 루프 (+ TradingView 웹훅 수신)
  engine replay [--start 2026-09-01] [--end ...]   저장된 시세로 전체 경로 모의 실행
  engine status | halt "사유" | resume
"""
import argparse
import json
import logging
import os
import signal as _signal
import sys
import threading
import time
from datetime import datetime, timezone

from . import config as config_mod
from .data.collector import Collector
from .data.exchanges import make_exchange
from .data.store import Store
from .engine.engine import Engine
from .engine.journal import Journal
from .engine.models import Signal
from .engine.paper import PaperBroker


def _list(s):
    return [x.strip().upper() for x in s.split(",") if x.strip()]


def _ms(s):
    if not s:
        return None
    return int(datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp() * 1000)


def _fmt_ts(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M") if ms else "-"


def _ensure_dir(path):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)


def make_broker(cfg, mode, journal):
    p = cfg.paper
    if mode == "paper":
        b = PaperBroker(equity=p.equity, taker_fee=p.taker_fee, maker_fee=p.maker_fee, slippage_bps=p.slippage_bps)
    elif mode == "gate-dryrun":
        from .engine.gate import GateDryRun
        b = GateDryRun(price_type=cfg.gate_live.price_type, equity=p.equity, taker_fee=p.taker_fee,
                       maker_fee=p.maker_fee, slippage_bps=p.slippage_bps)
    elif mode == "gate":
        from .engine.gate import GateBroker
        g = cfg.gate_live
        if g.dry_run:
            raise SystemExit("gate_live.dry_run = true 입니다. 실제 주문하려면 설정에서 false로 바꾸세요 (먼저 testnet).")
        b = GateBroker(os.environ.get(g.key_env), os.environ.get(g.secret_env), testnet=g.testnet, price_type=g.price_type)
    else:
        raise SystemExit(f"알 수 없는 모드: {mode} (paper, gate-dryrun, gate)")
    if hasattr(b, "load_state"):
        b.load_state(journal.get("broker_state"))
    return b


def cmd_collect(cfg, a):
    c = cfg.collector
    _ensure_dir(cfg.general.db)
    store = Store(cfg.general.db)
    col = Collector(store, make_exchange(a.exchange or c.exchange))
    symbols = _list(a.symbols) if a.symbols else list(c.symbols)
    intervals = [x.strip() for x in a.intervals.split(",")] if a.intervals else list(c.intervals)
    if a.action in ("update", "run") and c.universe_top:
        symbols = list(dict.fromkeys(symbols + col.universe(c.universe_top, c.min_quote_volume)))
    if a.action == "contracts":
        print(f"계약 정보 {col.sync_contracts()}개 저장")
    elif a.action == "update":
        if store.contract(col.ex.name, symbols[0]) is None:
            print(f"계약 정보 {col.sync_contracts()}개 저장")
        for (s, iv), n in col.cycle(symbols, intervals, c.lookback_days).items():
            print(f"{s:>8} {iv:>4}: {n}")
        for s in symbols:
            try:
                col.sync_funding(s)
            except Exception as e:
                print(f"{s} 펀딩비 실패: {e}")
    elif a.action == "backfill":
        for s in symbols:
            for iv in intervals:
                print(f"{s:>8} {iv:>4}: {col.backfill(s, iv, a.days)}")
    elif a.action == "repair":
        for s in symbols:
            for iv in intervals:
                print(f"{s:>8} {iv:>4}: 메운 봉 {col.repair(s, iv)}")
    elif a.action == "check":
        print(f"{'거래소':<6} {'종목':<8} {'봉':<4} {'개수':>8} {'처음 (UTC)':<17} {'마지막 (UTC)':<17} {'빠짐':>6} {'이상':>4}")
        for ex, s, iv, n, first, last in store.series():
            q = store.quality(ex, s, iv)
            print(f"{ex:<6} {s:<8} {iv:<4} {n:>8} {_fmt_ts(first):<17} {_fmt_ts(last):<17} {q['missing']:>6} {q['bad_ohlc']:>4}")
    elif a.action == "universe":
        print(", ".join(col.universe(a.top, c.min_quote_volume)))
    elif a.action == "run":
        stop = threading.Event()
        _signal.signal(_signal.SIGINT, lambda *_: stop.set())
        _signal.signal(_signal.SIGTERM, lambda *_: stop.set())
        print(f"수집 시작: {col.ex.name} {','.join(symbols)} {','.join(intervals)} (Ctrl+C 종료)")
        col.run_forever(symbols, intervals, c.lookback_days, c.every_seconds, should_stop=stop.is_set)


def _engine(cfg, mode, journal_mode=None, journal_path=None):
    path = journal_path or cfg.general.journal
    _ensure_dir(path)
    _ensure_dir(cfg.general.db)
    os.makedirs(cfg.general.state_dir, exist_ok=True)
    store = Store(cfg.general.db)
    journal = Journal(path, journal_mode or mode)
    broker = make_broker(cfg, mode, journal)
    return Engine(cfg, store, broker, journal)


def _print_decision(d):
    print(d.summary())
    for c in d.checks:
        print(f"  {'✓' if c.ok else '✗'} {c.code:<16} {c.detail}")


def cmd_engine(cfg, a):
    mode = a.mode or cfg.engine.mode
    if a.action in ("risk", "submit"):
        eng = _engine(cfg, mode)
        side = {"long": 1, "short": -1}[a.side]
        price, age = eng._market(a.symbol.upper())
        sig = Signal(symbol=a.symbol.upper(), side=side, price=a.price or price, ts_ms=eng.now(), stop=a.stop,
                     tf=cfg.engine.tf, score=None, l1=a.l1, source="cli")
        if a.action == "risk":
            # 주문 없이 점검만: 엔진의 submit 경로에서 브로커 호출만 빼고 같은 계산
            from .engine import signals as sg
            if sig.stop is None or sig.l1 is None:
                df = eng.store.load(eng.ex, sig.symbol, eng.tf, limit=cfg.engine.history_bars)
                hdf = eng.store.load(eng.ex, sig.symbol, eng.htf, limit=600)
                stop, l1, _ = sg.structural_stop_now(df, hdf, side, eng.htf)
                sig.stop = sig.stop if sig.stop is not None else stop
                sig.l1 = sig.l1 if sig.l1 is not None else l1
            d = eng.risk.check(sig, eng.context(sig.symbol, a.price or price, age))
            _print_decision(d)
            if d.plan:
                print(json.dumps(d.plan.to_dict(), ensure_ascii=False, indent=1))
        else:
            d = eng.submit(sig, price=a.price or price, data_age_s=age)
            _print_decision(d)
            if hasattr(eng.broker, "save_state"):
                eng.journal.set("broker_state", eng.broker.save_state())
    elif a.action == "run":
        eng = _engine(cfg, mode)
        stop = threading.Event()
        _signal.signal(_signal.SIGINT, lambda *_: stop.set())
        _signal.signal(_signal.SIGTERM, lambda *_: stop.set())
        if a.webhook:
            from .engine.webhook import serve
            srv = serve(eng, cfg.webhook.host, cfg.webhook.port, os.environ.get(cfg.webhook.token_env))
            threading.Thread(target=srv.serve_forever, daemon=True).start()
        print(f"엔진 시작: {mode} {cfg.engine.exchange} {','.join(cfg.engine.symbols)} {cfg.engine.tf} (Ctrl+C 종료)")
        while not stop.is_set():
            try:
                n = eng.step()
                if n:
                    print(f"{datetime.now().strftime('%H:%M:%S')} 새 봉 {n}개 처리, 보유 {len(eng.broker.open_positions())}")
            except Exception as e:
                logging.exception("엔진 오류: %s", e)
            stop.wait(cfg.engine.poll_seconds)
    elif a.action == "replay":
        tag = f"replay-{int(time.time())}"
        path = a.journal or os.path.join(cfg.general.state_dir, f"{tag}.db")
        eng = _engine(cfg, "paper", journal_mode=tag, journal_path=path)
        eng.broker.load_state(None)
        syms = _list(a.symbols) if a.symbols else None
        res = eng.replay(syms, _ms(a.start), _ms(a.end))
        print(json.dumps(res, ensure_ascii=False, indent=1, default=float))
        print(f"기록: {path} (mode={tag})")
    elif a.action == "status":
        eng = _engine(cfg, mode)
        h, why = eng.halted()
        print(f"모드 {mode} | 정지 {'예: ' + why if h else '아니오'} | 자산 {eng.broker.equity(eng.last_px):,.2f}")
        for p in eng.broker.open_positions():
            print(f"  #{p.id} {p.symbol} {'롱' if p.side > 0 else '숏'} {p.qty:g} @ {p.entry_price:g} 손절 {p.stop_price:g} "
                  f"청산가 {p.liq_price:g} 1차익절 {'완료' if p.tp1_done else p.tp1_price}")
        for ts, ok, s in eng.journal.recent_decisions(10):
            print(f"  {_fmt_ts(ts)} {'✓' if ok else '✗'} {s}")
    elif a.action == "halt":
        _engine(cfg, mode).halt(a.reason or "수동 정지")
        print("신규 진입을 정지했습니다 (보유 포지션 관리는 계속)")
    elif a.action == "resume":
        _engine(cfg, mode).resume()
        print("재개했습니다")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="ppp2", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-c", "--config", default=os.environ.get("PPP2_CONFIG"), help="설정 파일 (TOML)")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("collect", help="선물 시세 수집")
    c.add_argument("action", choices=["contracts", "update", "backfill", "repair", "check", "universe", "run"])
    c.add_argument("-s", "--symbols")
    c.add_argument("-i", "--intervals")
    c.add_argument("-x", "--exchange")
    c.add_argument("--days", type=float, default=30)
    c.add_argument("--top", type=int, default=20)
    e = sub.add_parser("engine", help="위험 점검 실행 엔진")
    e.add_argument("action", choices=["risk", "submit", "run", "replay", "status", "halt", "resume"])
    e.add_argument("reason", nargs="?")
    e.add_argument("-m", "--mode", help="paper | gate-dryrun | gate")
    e.add_argument("-s", "--symbol", "--symbols", dest="symbol")
    e.add_argument("--side", choices=["long", "short"])
    e.add_argument("--price", type=float)
    e.add_argument("--stop", type=float)
    e.add_argument("--l1", type=int)
    e.add_argument("--webhook", action="store_true")
    e.add_argument("--start")
    e.add_argument("--end")
    e.add_argument("--journal")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO if a.verbose else logging.WARNING, format="%(asctime)s %(name)s %(message)s")
    cfg = config_mod.load(a.config)
    if a.cmd == "collect":
        cmd_collect(cfg, a)
    else:
        if a.action in ("risk", "submit") and (not a.symbol or not a.side):
            ap.error("risk/submit에는 -s 종목과 --side가 필요합니다")
        a.symbols = a.symbol
        cmd_engine(cfg, a)


if __name__ == "__main__":
    sys.exit(main())
