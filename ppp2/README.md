# PPP 2.0: 선물 시세 수집기와 위험 점검 실행 엔진

「PPP 2.0 개발 기획서」의 단계 1(데이터)과 L2(비중·위험)·L4(실행)·청산 관리 모듈을 구현한 Python 패키지입니다. 기획서와 사례 검증 결과는 [`reports/ppp-2.0-plan.html`](../reports/ppp-2.0-plan.html), [`reports/ppp-case-audit.html`](../reports/ppp-case-audit.html)에 있습니다.

> ⚠️ 매매 추천이 아닙니다. 지금까지의 검증에서 PPP 신호 자체의 우위는 확인되지 않았습니다. 이 엔진의 목적은 **어떤 신호가 오든 계좌가 한 번에 크게 다치지 않게 막는 것**입니다. 실거래(`mode = "gate"`)는 테스트넷에서 먼저 확인하세요. Gate 실주문 코드는 가짜 응답으로만 시험했고 실계정 키로는 검증하지 않았습니다.

## 구성

```
ppp2/
  core/        PPP 지표 (pine-scripts/ppp/verify와 같은 코드, 테스트로 일치 확인)
  data/        http.py  exchanges.py (Gate, BingX)  store.py (SQLite)  collector.py
  engine/      risk.py (위험 점검)  paper.py (모의 체결)  manager.py (청산 관리)  signals.py (PPP v2 신호)
               engine.py (전체 흐름)  journal.py (기록)  gate.py (Gate 주문·드라이런)  webhook.py (TradingView 알림)
  cli.py       python -m ppp2 ...
tests/         64개 (네트워크 없이 실행)
```

```
거래소 API ──► 수집기 ──► market.db ──► 엔진: 새 봉 → 체결 처리 → 청산 관리 → PPP v2 신호
                                               ▲                                    │
TradingView 알림 ──► 웹훅 ─────────────────────┴──────► 위험 점검 ─(승인)─► 브로커(paper / gate-dryrun / gate)
                                                          │                         │
                                                          └──── engine.db (신호·점검 결과·포지션·체결·자산) ◄─┘
```

## 설치와 실행

```bash
cd ppp2
pip install -r requirements.txt
cp config.example.toml config.toml          # 필요한 값만 고치기

# 1) 시세 수집
python -m ppp2 -c config.toml collect update     # 계약 정보 + 처음 받기 (BTC·ETH·SOL 1m/15m/4h 약 45초)
python -m ppp2 -c config.toml collect check      # 빠진 봉·이상 봉 점검
python -m ppp2 -c config.toml collect run        # 1분마다 이어 받기 (상시 실행)

# 2) 위험 점검만 해 보기 (주문 없음)
python -m ppp2 -c config.toml engine risk -s BTC --side long             # 손절가는 구조적 손절가로 자동
python -m ppp2 -c config.toml engine risk -s BTC --side long --stop 79000

# 3) 저장된 시세로 전체 경로 모의 실행
python -m ppp2 -c config.toml engine replay

# 4) 실시간 모의 매매 + TradingView 웹훅
export PPP2_WEBHOOK_TOKEN=아무도-모르는-문자열
python -m ppp2 -c config.toml engine run --webhook               # mode=paper
python -m ppp2 -c config.toml engine run --webhook -m gate-dryrun  # Gate에 보낼 요청을 기록만

python -m ppp2 -c config.toml engine status
python -m ppp2 -c config.toml engine halt "점검"    # 신규 진입 정지 (보유 포지션 관리는 계속)
python -m ppp2 -c config.toml engine resume
```

수집기(`collect run`)와 엔진(`engine run`)은 따로 띄웁니다. 같은 `market.db`를 쓰고, SQLite WAL 모드라 동시에 읽고 쓸 수 있습니다.

## 선물 시세 수집기

| 기능 | 내용 |
|---|---|
| 거래소 | Gate, BingX 무기한 선물 (바이낸스·바이비트는 이 환경에서 접속 차단) |
| 이어 받기 | 마지막 저장 봉의 2개 앞부터 다시 받아 덮어씀. 처음이면 `lookback_days`만큼 |
| 마감된 봉만 | 아직 진행 중인 봉은 저장하지 않음 |
| Gate 1만 봉 제한 | 요청 시작을 자동으로 잘라 오류 없이 받음. 그보다 오래된 이력은 상시 수집으로 쌓아야 합니다 |
| 빠진 봉 | `repair`가 다시 요청 (30분마다 자동), `check`가 개수 보고 |
| 계약 정보 | 유지 증거금률, 최대 레버리지, 수수료, 계약 크기, 호가 단위를 저장 → 위험 점검이 사용 |
| 펀딩비 | 종목별 이력 저장 |
| 장애 | 호출 제한·5xx는 지수 백오프로 재시도. 한 종목 오류가 나머지를 멈추지 않음 |
| 종목 선정 | `universe_top = N`이면 24시간 거래대금 상위 N개 추가 |

## 위험 점검 (주문 전 19개 항목, 점수 기준을 켜면 20개)

모든 항목을 평가해서 실패 이유를 한꺼번에 기록합니다. 하나라도 실패하면 주문하지 않습니다.

| 코드 | 내용 | 사례 근거 |
|---|---|---|
| `STOP_BEFORE_LIQ` | 손절 거리 + 유지증거금률 ≤ 0.8 / 레버리지. 청산가가 손절가보다 바깥 | 사례 4건(LSK·FF·ROBO·4)에서 위반 |
| `LEVERAGE` | 레버리지 = min(청산 안전 배수, 상한, 거래소 최대). 증거금이 계좌 25%를 넘지 않는 가장 낮은 배수 선택 | 평균 31배 사용 |
| `SIZE` | 계좌 1% 위험(수수료 포함) ÷ 손절 거리로 수량 계산, 계약 단위 내림, 최소 수량 확인 | 계좌 위험 10~100% |
| `MARGIN_MODE` | 격리 마진만 | Gate 카드 대부분 교차 마진 |
| `L1` | 엔진이 4시간봉으로 계산한 방향과 같아야 함. 외부 신호의 L1 값은 믿지 않고 다시 계산 | 4건이 1시간봉과 반대 |
| `COST` | 1차 익절 거리 ≥ 왕복 시장가 수수료 × 3 | 1분봉은 수수료가 ATR보다 큼 |
| `STOP` / `STOP_RANGE` | 손절가가 올바른 쪽에, 0.1~5% 범위 | |
| `DAILY_LOSS` / `WEEKLY_LOSS` | 오늘 −2%, 이번 주 −5%면 신규 진입 없음 (다음 날·다음 주 자동 해제) | |
| `MAX_OPEN` / `DUPLICATE` / `EXPOSURE` | 동시 3개, 같은 종목 중복 금지, 전체 명목가 ≤ 계좌 × 3 | |
| `SIGNAL_AGE` / `DATA_AGE` / `PRICE_DEV` | 오래된 신호·시세, 현재가와 0.3% 넘게 다른 신호 거부 | |
| `HALT` / `CONTRACT` / `SIDE` | 수동 정지, 계약 정보 없음, 방향 오류 | |

진입이 승인되면 같은 순간에 손절 주문(남은 물량 전체)과 1차 익절 지정가 주문을 냅니다. 사례 08 ROBO처럼 주문 없이 보유하는 일이 없게 하기 위해서입니다. 이후 청산 관리가 1차 익절 → 본전 이동 → EMA(20) 추적 청산 → 시간 청산을 맡습니다.

Gate 실주문(`mode = "gate"`)에는 안전장치가 하나 더 있습니다. 진입 직후 거래소가 계산한 실제 청산가를 다시 읽습니다. 손절가가 그보다 바깥이거나 손절 주문이 실패하면 즉시 시장가로 정리하고 엔진을 정지합니다.

## 실데이터 확인 결과 (2026-10-24)

- **수집:** Gate BTC·ETH·SOL 1m 8,640봉, 15m 9,600봉, 4h 2,400봉을 약 45초에 받았습니다. 빠진 봉 0, 이상 봉 0입니다. 계약 정보는 1,061개 저장했습니다.
- **모의 실행:** 15분봉 100일(3종목)을 `engine replay`로 돌렸습니다.
  - 신호 6,965건 중 173건 승인. 거부 사유는 L1 4,194건, 비용 2,490건, 손절 방향 2,387건 등입니다.
  - 거래 172건, 강제 청산 0건, 손절이 청산보다 늦은 주문 0건.
  - 레버리지 1~7배(평균 4.7배), 최소 청산 거리 13.8%, 최악 거래 −1.06%.
  - 전략 자체는 100일 동안 −35%였습니다(승률 43%). 위험 한도는 지켜졌지만 신호에 우위가 없다는 기존 검증과 같은 결과입니다.
- **실시간 연결:** `engine run --webhook -m gate-dryrun`으로 TradingView 형식 알림을 보냈습니다.
  - 토큰이 틀리면 401로 거부했습니다.
  - 알림이 L1=+1이라고 보냈지만 엔진의 4시간봉 L1이 −1이라 거부했습니다.
  - 승인된 경우에는 레버리지 설정 → 시장가 진입 → 손절 조건부 주문 → 1차 익절 지정가 순서의 Gate 요청이 기록됐습니다.

## 테스트

```bash
cd ppp2 && python -m pytest -q        # 64 passed, 네트워크 불필요
```

위험 점검(사례 LSK·ROBO 수치 포함), 모의 체결(손절·1차 익절·본전·갭 청산), 청산 관리, 저장소·수집기(이어 받기·빠진 봉·장애 격리), 거래소 어댑터(Gate 1만 봉 제한, BingX 호출 제한), Gate 서명·요청 형식·안전장치(청산가 재확인, 손절 주문 실패 시 정리, 양방향 모드 거부), 웹훅(토큰·종목명 정리), 엔진 전체 경로를 다룹니다. 연구 코드와 패키지 지표 코드가 같은 값을 내는지도 확인합니다.

## 아직 없는 것

- Gate 실계정·테스트넷 검증 (테스트넷 API가 이 환경에서 502 응답)
- BingX 주문 연결 (시세 수집만 지원)
- 지정가 진입(현재 시장가 진입, 1차 익절만 지정가), 부분 체결 처리
- 레이더 웹 화면, 텔레그램 알림
