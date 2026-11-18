# Grok Desk — Polymarket BTC Up/Down 리서치 봇 (모의매매 우선)

Instagram에 돌던 "Grok으로 만든 Polymarket AI 트레이딩 시스템" 게시물의 구조를 그대로 옮기되,
**과장된 부분은 빼고 검증 가능한 형태**로 다시 만든 프로젝트입니다.

- 기본값은 **모의매매(paper)**: 실제 호가창을 걸어 내려가며 가상 체결하고, 실제 테이커 수수료를 뺍니다.
- **Grok은 "방아쇠"가 아니라 "거부권"만 가진 감독관**입니다. 1초 단위 가격 결정은 수학 모델이 하고,
  Grok은 퀀트 에이전트가 이미 제안한 거래를 승인(APPROVE)/거부(VETO)만 합니다. 하루 예산 상한이 있고,
  오류가 나면 기본적으로 거래하지 않습니다(fail closed).
- 실거래(live)는 설정·환경변수·지역 확인 3중 잠금을 모두 풀어야만 켜집니다. **Polymarket이 제한하는
  지역에서는 거부되며, 지역 제한을 우회하는 기능은 넣지 않았습니다.**

> ⚠️ 투자 조언이 아닙니다. 예측시장은 원금 전부를 잃을 수 있고, 국가에 따라 이용 자체가 불법(한국에서는
> 해외 베팅 사이트 이용이 도박 관련 법률에 저촉될 소지)일 수 있습니다. 실거래 전 본인 지역의 법규와
> Polymarket 이용약관을 반드시 확인하세요.

## 게시물의 "에이전트 6개" ↔ 이 코드

| 게시물 | 코드 | 하는 일 |
|---|---|---|
| SPOTTER | `Desk.discover` | `btc-updown-{5m,15m,1h}-{시작시각}` 슬러그로 현재 열린 창(window)을 찾음 |
| PRIOR | `agents.PriorAgent` + `quant.prob_up` | 현물가·실현변동성·남은 시간으로 P(UP) 계산 (60초 TWAP 정산 반영) |
| EDGE | `agents.EdgeAgent` | 공정확률 − (지정가 + 테이커 수수료). 기준 미달이면 패스 |
| KELLY | `agents.KellyAgent` | ¼ 켈리, 자금의 2%·$25·호가 깊이 25% 상한 |
| TAKER | `execution.PaperExecutor` / `LiveExecutor` | Fill-or-Kill 매수 (모의 / 실제) |
| CLOSER | `Desk.settle_due` | 만기 후 Gamma 정산값으로 손익 확정, 없으면 현물 TWAP 추정 |
| GROK (core) | `grok.GrokSupervisor` | 거래 제안을 보고 승인/거부만. 비용 추적 |
| THE TRACE | `data/<mode>/trace.jsonl` | 모든 판단을 한 줄씩 기록 |

리스크 한도(`risk.py`)는 어떤 에이전트도, Grok도 넘을 수 없습니다: 일일 손실 한도, 동시 포지션 수,
시장당 1포지션, 만기 직전·너무 이른 시점 진입 금지, `STOP` 파일 킬스위치.

## 만들면서 확인한 사실 (2026년 11월, 실제 API 기준)

1. **정산 기준은 Chainlink BTC/USD 60초 TWAP**입니다 (`cryptoMarketConfig.twapLookbackSeconds = 60`).
   게시물처럼 "바이낸스 가격"이 아닙니다. 그래서 모델은 바이낸스를 대용(proxy)으로 쓰되
   `basis_bps` 만큼 불확실성을 더하고, 마지막 60초는 이미 확정된 평균을 반영합니다.
2. **테이커 수수료가 있습니다** (`feeSchedule: rate 0.07, exponent 1, takerOnly`). 이 코드는
   주당 수수료 = `0.07 × p × (1−p)`로 해석합니다(50¢에서 1.75¢로 최대). Polymarket 문서로 재확인하세요.
3. **CLOB 호가창은 최우선 호가가 배열 맨 뒤**에 옵니다. 정렬하지 않으면 최악가를 최우선가로 착각합니다.
4. **시장은 이미 꽤 효율적**입니다. `scan`으로 본 실제 15분 창에서 모델 0.839 vs 시장 0.84/0.85 —
   수수료와 1틱 슬리피지를 빼면 양쪽 다 음(−)의 기대값이었습니다. 45초 모의 운용에서도 진입 0건.
5. **게시물이 비법이라던 "회귀 기울기 + R² 필터"는 오히려 예측을 망쳤습니다.**
   최근 96개 15분 창을 1초봉으로 재현한 결과 (Brier 점수, 낮을수록 좋음, 0.25 = 동전 던지기):

   | 만기 전 | drift 사용 | drift 미사용 |
   |---|---|---|
   | 600초 | 0.242 | **0.188** |
   | 300초 | 0.171 | **0.106** |
   | 120초 | 0.067 | **0.050** |
   | 60초 | 0.036 | **0.028** |
   | 30초 | 0.019 | **0.018** |

   그래서 `drift_weight` 기본값은 0(꺼짐)입니다. 켜기 전에 `calibrate`로 직접 다시 확인하세요.

## 설치 · 실행

Python 3.11+만 있으면 됩니다(표준 라이브러리만 사용). 이 폴더 안에서:

```bash
python -m unittest discover -s tests -t .      # 테스트 47개, 네트워크 불필요
cp config.example.toml config.toml              # 필요하면 수정

python -m grokdesk -c config.toml scan          # 읽기 전용: 현재 창, 모델 vs 시장, 수수료, 지역 차단 여부
python -m grokdesk -c config.toml paper         # 모의매매 루프 (Ctrl+C로 종료)
python -m grokdesk -c config.toml stats         # 장부 요약: 승률, 실현손익, 모델 기대손익, 수수료
python -m grokdesk -c config.toml calibrate --windows 96   # 모델 보정 검증 (약 4분)
```

Grok 감독관 켜기: `config.toml`에서 `[grok] enabled = true`, 그리고 `export XAI_API_KEY=...`.
모델명·토큰 단가는 xAI 최신 값으로 맞추세요(예산 계산에 쓰임).

### 실거래 (권장하지 않음)

다음을 **모두** 만족해야 `live`가 시작됩니다. 하나라도 빠지면 거부합니다.

1. `config.toml`의 `[desk] mode = "live"`
2. `GROKDESK_LIVE_CONFIRM=I_ACCEPT_REAL_MONEY_RISK`
3. `https://polymarket.com/api/geoblock` 결과가 `blocked: false`
4. `POLY_PRIVATE_KEY`, `POLY_FUNDER` 환경변수, 그리고 `pip install -r requirements-live.txt`

`LiveExecutor`는 Polymarket 공식 `py-clob-client`를 쓰지만 **이 저장소에서 실제 거래소로 검증되지
않았습니다**(개발 환경이 지역 차단됨). 최소 주문 단위로 먼저 시험하세요.

## 이 코드가 증명하지 못하는 것

- `calibrate`는 **확률이 잘 맞는지**만 봅니다. 과거 호가창이 없어서 "시장보다 먼저 알았는지",
  즉 실제 수익성은 측정하지 못합니다. 그건 `paper` 모드를 충분히 오래 돌려 `stats`의
  `realized_pnl`과 `model_expected_pnl`을 비교해야 알 수 있습니다.
- 바이낸스와 Chainlink 사이 차이(basis), 주문 지연, 경쟁 봇은 모의 체결에 반영되지 않습니다.
  모의 손익은 실거래보다 **낙관적**이라고 보세요.
