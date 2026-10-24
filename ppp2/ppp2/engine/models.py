"""엔진에서 주고받는 데이터."""
from dataclasses import asdict, dataclass, field


@dataclass
class Signal:
    symbol: str                   # 기초자산 ("BTC")
    side: int                     # +1 롱, -1 숏
    price: float                  # 신호 봉 종가
    ts_ms: int                    # 신호 봉 마감 시각
    stop: float | None = None     # 없으면 엔진이 구조적 손절가를 계산
    tf: str = ""
    score: float | None = None
    l1: int | None = None         # 신호 시점 L1 방향 (+1/0/-1)
    source: str = "manual"

    def to_dict(self):
        return asdict(self)


@dataclass
class Check:
    code: str
    ok: bool
    detail: str


@dataclass
class OrderPlan:
    symbol: str
    side: int
    entry_price: float
    qty: float                    # 계약 수
    notional: float
    leverage: int
    margin: float
    stop_price: float
    tp1_price: float
    tp1_qty: float
    liq_price: float
    stop_dist: float              # 가격 대비 비율
    liq_dist: float
    risk_amount: float            # 손절 시 예상 손실 (수수료 포함)
    fee_est: float
    mmr: float

    def to_dict(self):
        return asdict(self)


@dataclass
class Decision:
    signal: Signal
    approved: bool
    checks: list = field(default_factory=list)
    plan: OrderPlan | None = None

    @property
    def failed(self):
        return [c for c in self.checks if not c.ok]

    def summary(self):
        if self.approved:
            p = self.plan
            return (f"승인 {self.signal.symbol} {'롱' if p.side > 0 else '숏'} {p.qty:g}계약 {p.leverage}배 "
                    f"진입 {p.entry_price:g} 손절 {p.stop_price:g}({p.stop_dist*100:.2f}%) 청산가 {p.liq_price:g}({p.liq_dist*100:.2f}%)")
        return "거부 " + self.signal.symbol + ": " + "; ".join(f"{c.code} {c.detail}" for c in self.failed)


@dataclass
class Position:
    id: int
    symbol: str
    side: int
    entry_price: float
    qty: float                    # 현재 계약 수
    init_qty: float
    leverage: int
    margin: float
    stop_price: float
    tp1_price: float
    tp1_qty: float
    liq_price: float
    opened_ms: int
    tf: str
    entry_bar_ts: int
    contract_size: float = 1.0
    tp1_done: bool = False
    realized: float = 0.0
    fees: float = 0.0
    status: str = "open"          # open | closed
    exit_reason: str = ""
    closed_ms: int | None = None
    exchange_ref: dict = field(default_factory=dict)

    @property
    def notional(self):
        return self.qty * self.contract_size * self.entry_price
