from dataclasses import dataclass
from typing import Optional

@dataclass(frozen=True)
class Decision:
    side: str
    ask: float
    reason: str

def choose_side(up_ask: Optional[float], down_ask: Optional[float], threshold: float = 0.80):
    candidates = []
    for side, ask in (("UP", up_ask), ("DOWN", down_ask)):
        if ask is not None and threshold <= float(ask) < 1.0:
            candidates.append((side, float(ask)))
    if not candidates:
        return None
    side, ask = max(candidates, key=lambda x: x[1])
    return Decision(side, ask, f"{side} best ask {ask:.4f} meets threshold {threshold:.0%}")

def paper_buy(cash: float, ask: float, allocation: float = 1.0, fee_rate: float = 0.0):
    if cash <= 0 or not 0 < ask < 1 or not 0 < allocation <= 1 or not 0 <= fee_rate < 1:
        return None
    spent = cash * allocation
    fee = spent * fee_rate
    return {"cash_spent": spent, "fee": fee, "shares": (spent-fee)/ask, "entry_price": ask}

def settle(shares: float, selected_side: str, resolved_outcome: str) -> float:
    if resolved_outcome not in {"UP", "DOWN"}:
        raise ValueError("Outcome must be UP or DOWN")
    return shares if selected_side == resolved_outcome else 0.0
