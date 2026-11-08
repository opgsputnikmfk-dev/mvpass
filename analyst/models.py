from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Optional

MINUTE = 60000

@dataclass
class Check:
    passed: Optional[bool]
    value: object
    detail: str

@dataclass
class Candidate:
    id: str
    symbol: str
    ts: int  # all source candles are CLOSED at this timestamp
    setup: str
    side: int
    reference: float
    stop: float
    target: float
    atr: float
    checks: dict[str, Check]
    levels: dict
    explanation: str
    structural_ok: bool = True

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, d):
        d = dict(d)
        d["checks"] = {k: Check(**v) for k, v in d["checks"].items()}
        return cls(**d)

@dataclass
class Position:
    id: str
    candidate_id: str
    symbol: str
    side: int
    entry_ts: int
    entry: float
    stop: float
    initial_stop: float
    target: float
    qty: float
    planned_risk: float
    entry_fee: float
    config_version: str
    rules: dict
    funding_paid: float = 0.0
    ambiguous_bars: int = 0
    exit_ts: Optional[int] = None
    exit_price: Optional[float] = None
    net_pnl: Optional[float] = None
    reason: str = "OPEN"
    last_bar: int = 0
    setup: str = ""
    target1: Optional[float] = None
    tp1_qty: float = 0.0
    tp1_exit_ts: Optional[int] = None
    tp1_exit_price: Optional[float] = None
    partial_gross: float = 0.0
    partial_fee: float = 0.0
    partial_risk: float = 0.0

    def to_dict(self):
        return asdict(self)
