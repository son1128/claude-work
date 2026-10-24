"""선물 시세 수집기.

  - update(): 저장된 마지막 봉 직전부터 지금까지 이어 받기 (처음이면 lookback일만큼)
  - repair(): 저장된 구간 안의 빠진 봉을 다시 받아 메우기
  - sync_contracts(), sync_funding(): 계약 정보(유지 증거금률·최대 레버리지·수수료·주문 단위)와 펀딩비
  - universe(): 24시간 거래대금 상위 종목
  - run_forever(): 정해진 주기로 위 작업 반복. 한 종목에서 오류가 나도 나머지는 계속합니다.
"""
import logging
import time

from .exchanges import INTERVAL_MS

log = logging.getLogger("ppp2.collector")


class Collector:
    def __init__(self, store, exchange, now=None, sleep=time.sleep):
        self.store = store
        self.ex = exchange
        self.now = now or (lambda: int(time.time() * 1000))
        self._sleep = sleep

    def sync_contracts(self):
        specs = self.ex.contracts()
        n = self.store.upsert_contracts(specs, self.now())
        self.store.log(self.now(), self.ex.name, "*", "", "contracts", n)
        return n

    def sync_funding(self, symbol):
        rows = self.ex.funding(symbol)
        return self.store.upsert_funding(self.ex.name, symbol, rows)

    def update(self, symbol, interval, lookback_days):
        step = INTERVAL_MS[interval]
        last = self.store.last_ts(self.ex.name, symbol, interval)
        start = (self.now() - int(lookback_days * 86_400_000)) // step * step if last is None else last - 2 * step
        rows = self.ex.candles(symbol, interval, start, self.now())
        n = self.store.upsert_candles(self.ex.name, symbol, interval, rows)
        self.store.log(self.now(), self.ex.name, symbol, interval, "update", n)
        return n

    def backfill(self, symbol, interval, days):
        """저장된 첫 봉보다 앞쪽을 days일 전까지 채웁니다 (Gate는 최근 1만 봉까지만 가능)."""
        step = INTERVAL_MS[interval]
        first = self.store.first_ts(self.ex.name, symbol, interval)
        target = (self.now() - int(days * 86_400_000)) // step * step
        if first is not None and first <= target:
            return 0
        rows = self.ex.candles(symbol, interval, target, (first - step) if first else self.now())
        n = self.store.upsert_candles(self.ex.name, symbol, interval, rows)
        self.store.log(self.now(), self.ex.name, symbol, interval, "backfill", n)
        return n

    def repair(self, symbol, interval, max_ranges=50):
        """빠진 봉 구간을 다시 요청합니다. 거래소에도 없는 봉(거래 중단 등)은 그대로 남습니다."""
        filled = 0
        for a, b, _ in self.store.gaps(self.ex.name, symbol, interval)[:max_ranges]:
            rows = self.ex.candles(symbol, interval, a, b)
            filled += self.store.upsert_candles(self.ex.name, symbol, interval, rows)
        if filled:
            self.store.log(self.now(), self.ex.name, symbol, interval, "repair", filled)
        return filled

    def universe(self, top_n=20, min_quote_volume=0.0):
        tick = self.ex.tickers()
        active = {s.symbol for s in self.ex.contracts() if s.active}
        ranked = sorted((t for t in tick.values() if t.symbol in active and t.quote_volume_24h >= min_quote_volume),
                        key=lambda t: t.quote_volume_24h, reverse=True)
        return [t.symbol for t in ranked[:top_n]]

    def cycle(self, symbols, intervals, lookback_days, repair=False):
        """수집 한 바퀴. {(symbol, interval): 새 봉 수 또는 오류 문자열}."""
        out = {}
        for s in symbols:
            for iv in intervals:
                try:
                    out[(s, iv)] = self.update(s, iv, lookback_days.get(iv, 7))
                    if repair:
                        out[(s, iv)] += self.repair(s, iv)
                except Exception as e:                       # 한 종목 실패가 전체를 멈추지 않게
                    out[(s, iv)] = f"오류: {e}"
                    log.warning("수집 실패 %s %s: %s", s, iv, e)
                    self.store.log(self.now(), self.ex.name, s, iv, "error", 0, str(e)[:500])
        return out

    def run_forever(self, symbols, intervals, lookback_days, every_s=60, contracts_every_s=3600,
                    repair_every_s=1800, should_stop=lambda: False):
        last_contracts = last_repair = 0.0
        while not should_stop():
            t0 = time.time()
            if t0 - last_contracts >= contracts_every_s:
                try:
                    self.sync_contracts()
                    for s in symbols:
                        self.sync_funding(s)
                except Exception as e:
                    log.warning("계약·펀딩 동기화 실패: %s", e)
                last_contracts = t0
            do_repair = t0 - last_repair >= repair_every_s
            res = self.cycle(symbols, intervals, lookback_days, repair=do_repair)
            if do_repair:
                last_repair = t0
            new = sum(v for v in res.values() if isinstance(v, int))
            errs = [k for k, v in res.items() if not isinstance(v, int)]
            log.info("수집 완료: 새 봉 %d개, 오류 %d건", new, len(errs))
            # 다음 분 경계 + 3초에 맞춰 깨어나 막 마감된 봉을 받습니다
            wait = max(1.0, every_s - (time.time() - t0))
            wait = min(wait, 60 - (time.time() % 60) + 3) if every_s <= 60 else wait
            self._sleep(wait)
