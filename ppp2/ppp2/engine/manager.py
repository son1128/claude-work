"""포지션 청산 관리 (기획서 '청산 관리' 모듈, ppp-v2-strategy.pine과 같은 규칙).

봉이 마감될 때마다
  1. 1차 익절이 체결됐으면 남은 물량의 손절을 진입가(본전)로 옮김
  2. 1차 익절 뒤 종가가 추적 EMA(trail_len) 반대편이면 나머지 시장가 청산
  3. 1차 익절 전 pre_tp_bars봉, 전체 max_bars봉이 지나면 시간 청산
1차 익절 물량이 0(나눌 수 없는 크기)이면 1차 익절가에 닿는 순간을 1차 익절로 보고 2·3을 적용합니다.
"""
from ..data.exchanges import INTERVAL_MS


class ExitManager:
    def __init__(self, exit_cfg):
        self.cfg = exit_cfg

    def on_bar_close(self, pos, bar, trail_value, broker, ts_ms):
        """bar = (ts, o, h, l, c). 취한 조치 문자열 목록을 돌려줍니다."""
        if pos.status != "open":
            return []
        acts = []
        ts, o, h, l, c = bar
        s = pos.side
        if not pos.tp1_done and pos.tp1_qty == 0 and s * ((h if s > 0 else l) - pos.tp1_price) >= 0:
            pos.tp1_done = True
            acts.append("1차 익절가 도달 (분할 없음)")
        if pos.tp1_done and s * (pos.stop_price - pos.entry_price) < 0:
            broker.modify_stop(pos, pos.entry_price, ts_ms)
            acts.append("손절을 본전으로")
        held = (ts - pos.entry_bar_ts) // INTERVAL_MS[pos.tf]
        if pos.tp1_done and trail_value is not None and s * (c - trail_value) < 0:
            broker.close(pos, c, ts_ms, "추적 청산")
            acts.append("추적 청산")
        elif (not pos.tp1_done and held >= self.cfg.pre_tp_bars) or held >= self.cfg.max_bars:
            broker.close(pos, c, ts_ms, "시간")
            acts.append("시간 청산")
        return acts
