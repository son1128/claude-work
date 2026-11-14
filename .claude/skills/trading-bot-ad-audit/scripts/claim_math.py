#!/usr/bin/env python3
"""수익 주장 검산기 — 홍보물의 "X를 Y로" 주장이 자기 자신이 밝힌 규칙 안에서 가능한지 계산한다.

예)
  python3 claim_math.py --start 44 --end 11643 --days 1 \
      --position 0.08 --stop 0.06 --win 1.0 --fee 0.02

인자:
  --start / --end   시작·종료 금액 (필수)
  --days            주장 기간(일). 연환산 수익률(CAGR) 계산용
  --position        1회 진입 최대 비중 (예: 0.08 = 잔고의 8%)
  --stop            손절 폭 (예: 0.06 = -6%)
  --win             승리 1회 평균 수익률 가정 (예: 1.0 = +100%)
  --fee             1회 왕복 비용(수수료+슬리피지+우선순위 수수료) 비율 가정 (예: 0.02)
  --gap             손절이 미끄러질 때 실제 손실 가정 (예: 0.5 = -50%, 밈코인 갭/러그)
표준 라이브러리만 사용, 네트워크 호출 없음.
"""
import argparse
import math


def fmt(x):
    return "{:,.2f}".format(x)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--start", type=float, required=True)
    p.add_argument("--end", type=float, required=True)
    p.add_argument("--days", type=float)
    p.add_argument("--position", type=float)
    p.add_argument("--stop", type=float)
    p.add_argument("--win", type=float, default=1.0)
    p.add_argument("--fee", type=float, default=0.0)
    p.add_argument("--gap", type=float)
    a = p.parse_args()

    if a.start <= 0 or a.end <= 0:
        p.error("금액은 0보다 커야 한다")

    mult = a.end / a.start
    print("== 주장 그대로 ==")
    print("배수: {:.1f}배  (수익률 {:+,.0f}%)".format(mult, (mult - 1) * 100))
    print("2배를 연속 몇 번 해야 하나: {:.1f}번".format(math.log(mult, 2)))

    if a.days:
        daily = mult ** (1 / a.days) - 1
        print("기간 {:g}일 → 하루 평균 {:+.1f}%".format(a.days, daily * 100))
        years = a.days / 365.0
        try:
            cagr = mult ** (1 / years) - 1
            print("연환산(CAGR): {:+.3g}%  (같은 속도가 1년 유지된다고 가정할 때)".format(cagr * 100))
        except OverflowError:
            print("연환산(CAGR): 계산 불가 수준(부동소수점 범위 초과) — 지속 불가능")

    if a.position:
        net_win = a.win - a.fee
        if net_win <= 0:
            print("\n승리 1회 순익이 0 이하 — 비용 가정이 수익보다 크다")
            return
        per_win = 1 + a.position * net_win
        need = math.log(mult) / math.log(per_win)
        print("\n== 자기 규칙 안에서 ==")
        print("진입 비중 {:.0%}, 승리 1회 {:+.0%}(비용 {:.1%} 차감 후 {:+.1%})".format(
            a.position, a.win, a.fee, net_win))
        print("→ 승리 1회당 잔고 x{:.4f}".format(per_win))
        print("→ 손실 없이 연속 {:.0f}번 이겨야 {:g}배 도달".format(math.ceil(need), round(mult, 1)))
        if a.stop:
            per_loss = 1 - a.position * (a.stop + a.fee)
            print("손절 1회(-{:.0%} + 비용)당 잔고 x{:.4f}".format(a.stop, per_loss))
            # 승률 w에서 1회 기대 로그성장 = w ln(per_win) + (1-w) ln(per_loss)
            lw, ll = math.log(per_win), math.log(per_loss)
            for w in (0.3, 0.4, 0.5, 0.6):
                g = w * lw + (1 - w) * ll
                if g > 0:
                    print("  승률 {:.0%}: 평균 {:,.0f}회 거래 필요".format(w, math.log(mult) / g))
                else:
                    print("  승률 {:.0%}: 기대 성장 ≤ 0 (영원히 도달 못 함)".format(w))
        if a.gap:
            per_gap = 1 - a.position * (a.gap + a.fee)
            print("손절이 미끄러져 -{:.0%}가 되면 1회당 잔고 x{:.4f} (손절 규칙이 보호하지 못하는 경우)".format(a.gap, per_gap))


if __name__ == "__main__":
    main()
