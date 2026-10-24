"""설정 (TOML). 파일에 없는 값은 아래 기본값을 씁니다. 예시는 config.example.toml."""
import tomllib
from dataclasses import dataclass, field, fields


@dataclass
class General:
    db: str = "data/market.db"              # 시세 DB (수집기가 씀)
    journal: str = "data/engine.db"          # 엔진 기록 DB
    state_dir: str = "data"                  # HALT 파일 등
    tz_offset_hours: int = 9                 # 일일 손실 한도의 "하루" 기준 (한국시간)


@dataclass
class CollectorCfg:
    exchange: str = "gate"
    symbols: list = field(default_factory=lambda: ["BTC", "ETH", "SOL"])
    intervals: list = field(default_factory=lambda: ["1m", "15m", "4h"])
    lookback_days: dict = field(default_factory=lambda: {"1m": 6, "5m": 30, "15m": 100, "1h": 400, "4h": 400, "1d": 1000})
    universe_top: int = 0                    # 0이면 symbols만, N이면 거래대금 상위 N개를 더함
    min_quote_volume: float = 20_000_000
    every_seconds: int = 60


@dataclass
class RiskCfg:
    risk_pct: float = 1.0                    # 1회 계좌 위험 (%)
    lev_cap: float = 10.0                    # 레버리지 상한
    liq_safety: float = 0.8                  # 손절 거리 + 유지증거금률 ≤ 0.8 / 레버리지
    max_margin_frac: float = 0.25            # 한 포지션 증거금 ≤ 계좌의 25%
    max_open: int = 3
    max_total_notional_x: float = 3.0        # 전체 명목가 ≤ 계좌 × 3
    daily_loss_pct: float = 2.0
    weekly_loss_pct: float = 5.0
    min_stop_pct: float = 0.10               # 너무 가까운 손절은 잡음에 걸림
    max_stop_pct: float = 5.0
    cost_gate_mult: float = 3.0              # 1차 익절 거리 ≥ 왕복 수수료 × 3
    max_signal_age_s: int = 180
    max_price_dev_pct: float = 0.30          # 신호 가격과 현재가 차이
    max_data_age_s: int = 600                # 시세 DB의 마지막 봉이 이보다 오래되면 거부
    require_l1: bool = True                  # L1(4시간봉) 방향과 같은 신호만
    min_score: float = 0.0                   # 점수는 검증상 효과가 없어 기본 0 (점검표 용도)
    isolated_only: bool = True


@dataclass
class ExitCfg:
    tp1_r: float = 1.0
    tp1_frac: float = 0.5
    trail_len: int = 20
    pre_tp_bars: int = 96
    max_bars: int = 288


@dataclass
class EngineCfg:
    mode: str = "paper"                      # paper | gate
    exchange: str = "gate"                   # 시세·계약 정보를 읽을 거래소 (DB 기준)
    symbols: list = field(default_factory=lambda: ["BTC", "ETH", "SOL"])
    tf: str = "15m"
    l1_tf: str = "4h"
    auto_signals: bool = True                # DB 시세로 PPP v2 신호를 직접 만들지
    poll_seconds: int = 20
    history_bars: int = 1500


@dataclass
class PaperCfg:
    equity: float = 10_000.0
    slippage_bps: float = 2.0
    taker_fee: float = 0.00075
    maker_fee: float = -0.0001


@dataclass
class GateLiveCfg:
    testnet: bool = True
    dry_run: bool = True                     # True면 주문 요청을 기록만 하고 보내지 않음
    key_env: str = "GATE_API_KEY"
    secret_env: str = "GATE_API_SECRET"
    price_type: int = 0                      # 손절 트리거 가격: 0 최종 체결가, 1 마크 가격


@dataclass
class WebhookCfg:
    host: str = "127.0.0.1"
    port: int = 8787
    token_env: str = "PPP2_WEBHOOK_TOKEN"


@dataclass
class Config:
    general: General = field(default_factory=General)
    collector: CollectorCfg = field(default_factory=CollectorCfg)
    risk: RiskCfg = field(default_factory=RiskCfg)
    exits: ExitCfg = field(default_factory=ExitCfg)
    engine: EngineCfg = field(default_factory=EngineCfg)
    paper: PaperCfg = field(default_factory=PaperCfg)
    gate_live: GateLiveCfg = field(default_factory=GateLiveCfg)
    webhook: WebhookCfg = field(default_factory=WebhookCfg)


def _fill(cls, data):
    known = {f.name for f in fields(cls)}
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"[{cls.__name__}] 알 수 없는 설정: {', '.join(sorted(unknown))}")
    return cls(**data)


def load(path=None):
    if not path:
        return Config()
    with open(path, "rb") as fh:
        raw = tomllib.load(fh)
    cfg = Config()
    for f in fields(Config):
        if f.name in raw:
            setattr(cfg, f.name, _fill(type(getattr(cfg, f.name)), raw[f.name]))
    unknown = set(raw) - {f.name for f in fields(Config)}
    if unknown:
        raise ValueError(f"알 수 없는 설정 구역: {', '.join(sorted(unknown))}")
    return cfg
