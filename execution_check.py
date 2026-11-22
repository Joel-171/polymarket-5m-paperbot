"""Read-only check for whether a budget fits at the current best ask."""
import argparse
import json
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_HALF_UP

import requests

from collector import CLOB, FIVE_MINUTE_SYMBOLS, SESSION, discover

SHARE_STEP = Decimal("0.01")
FEE_STEP = Decimal("0.00001")


def estimate_buy(book, budget, max_price, fee_rate):
    """Require enough depth at one price for the fee-adjusted order size."""
    if not budget.is_finite() or budget <= 0:
        raise ValueError("Budget must be positive and finite.")
    if not max_price.is_finite() or not 0 < max_price < 1:
        raise ValueError("Maximum price must be between 0 and 1.")
    if not fee_rate.is_finite() or not 0 <= fee_rate <= 1:
        raise ValueError("Invalid fee rate.")

    asks = [(Decimal(row["price"]), Decimal(row["size"])) for row in book["asks"]]
    asks = [(price, size) for price, size in asks if 0 < price < 1 and size > 0]
    if not asks:
        return {"fits": False, "reason": "No asks available."}
    best_ask = min(price for price, _ in asks)
    available_shares = sum(size for price, size in asks if price == best_ask)
    fee_per_share = fee_rate * best_ask * (1 - best_ask)
    # Leave room for the fee to round up without exceeding the budget.
    rounding_allowance = FEE_STEP / 2 if fee_rate else Decimal(0)
    spendable = max(Decimal(0), budget - rounding_allowance)
    shares = (spendable / (best_ask + fee_per_share)).quantize(SHARE_STEP, rounding=ROUND_DOWN)
    fee = (shares * fee_per_share).quantize(FEE_STEP, rounding=ROUND_HALF_UP)
    total = shares * best_ask + fee
    minimum_shares = Decimal(book["min_order_size"])
    reason = None
    if best_ask > max_price:
        reason = "Best ask exceeds the price cap."
    elif shares <= 0 or shares < minimum_shares:
        reason = "The budget cannot buy the minimum order size."
    elif available_shares < shares:
        reason = "Insufficient shares at the best ask for the full estimated order."
    return {
        "fits": reason is None,
        "reason": reason,
        "best_ask": best_ask,
        "available_shares": available_shares,
        "minimum_shares": minimum_shares,
        "estimated_order_shares": shares,
        "estimated_taker_fee": fee,
        "estimated_total_cost": total,
        "unused_budget": budget - total,
    }


def check_current_market(asset, side, budget, max_price):
    for _event, market, up_token, down_token, _start, end in discover():
        if market["slug"].startswith(f"{asset}-"):
            break
    else:
        raise ValueError(f"No current {asset.upper()} market is accepting orders.")

    if market.get("feesEnabled") is False:
        rate = Decimal(0)
    else:
        schedule = market.get("feeSchedule") or {}
        if market.get("feesEnabled") is not True or schedule.get("exponent") != 1:
            raise ValueError("Fee schedule is missing or uses an unsupported exponent.")
        rate = Decimal(str(schedule["rate"]))
    token = up_token if side == "UP" else down_token
    response = SESSION.get(f"{CLOB}/book", params={"token_id": token}, timeout=12)
    response.raise_for_status()
    book = response.json()
    if time.time() >= end:
        raise ValueError("The market expired during the check. Run it again for the next window.")
    result = estimate_buy(book, budget, max_price, rate)
    return {
        "market": market["slug"],
        "condition_id": market["conditionId"],
        "token_id": token,
        "side": side,
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "book_timestamp": book.get("timestamp"),
        "book_hash": book.get("hash"),
        "budget": budget,
        "max_price": max_price,
        "fee_rate": rate,
        **result,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("asset", type=str.lower, choices=FIVE_MINUTE_SYMBOLS)
    parser.add_argument("side", type=str.upper, choices=("UP", "DOWN"))
    parser.add_argument("budget")
    parser.add_argument("max_price")
    args = parser.parse_args()
    try:
        result = check_current_market(
            args.asset, args.side, Decimal(args.budget), Decimal(args.max_price),
        )
    except (ValueError, KeyError, InvalidOperation, requests.RequestException) as error:
        parser.exit(2, f"Execution check failed: {error}\n")
    print(json.dumps(result, indent=2, default=str))
    return 0 if result["fits"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
