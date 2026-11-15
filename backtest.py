"""Chronological paper backtest using manually verified UP/DOWN outcomes."""
import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path
from strategy import choose_side, paper_buy, settle


def number(value):
    try:
        return None if value in (None, "") else float(value)
    except (TypeError, ValueError):
        return None


def run(rows, initial_cash=100.0, threshold=.80, entry_window=120, allocation=1.0, fee_rate=0.0):
    grouped = defaultdict(list)
    for row in rows:
        if row.get("market_id") and row.get("resolved_outcome") in {"UP", "DOWN"}:
            grouped[row["market_id"]].append(row)
    cash, trades, skipped = initial_cash, [], 0
    for market_id, samples in grouped.items():
        samples.sort(key=lambda r: r.get("observed_at", ""))
        outcome = samples[-1]["resolved_outcome"]
        entry = None
        for row in samples:
            tte = number(row.get("seconds_to_end"))
            if tte is None or not 0 <= tte <= entry_window:
                continue
            decision = choose_side(number(row.get("up_best_ask")), number(row.get("down_best_ask")), threshold)
            if decision:
                entry = row, decision
                break
        if entry is None or cash <= 0:
            skipped += 1
            continue
        row, decision = entry
        fill = paper_buy(cash, decision.ask, allocation, fee_rate)
        if not fill:
            skipped += 1
            continue
        payout = settle(fill["shares"], decision.side, outcome)
        pnl = payout - fill["cash_spent"]
        cash += pnl
        trades.append({"market_id": market_id, "side": decision.side, "entry_ask": decision.ask,
                       "outcome": outcome, "pnl": pnl, "cash_after": cash})
    wins = sum(t["pnl"] > 0 for t in trades)
    return {"initial_cash": initial_cash, "final_cash": cash, "net_pnl": cash-initial_cash,
            "trade_count": len(trades), "wins": wins, "losses": len(trades)-wins,
            "win_rate": wins/len(trades) if trades else None, "skipped_markets": skipped, "trades": trades}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--input", required=True)
    p.add_argument("--cash", type=float, default=100)
    p.add_argument("--threshold", type=float, default=.80)
    p.add_argument("--entry-window", type=int, default=120)
    p.add_argument("--allocation", type=float, default=1.0)
    p.add_argument("--fee-rate", type=float, default=0.0)
    p.add_argument("--output", default="backtest_results.json")
    a = p.parse_args()
    with open(a.input, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows or not any(r.get("resolved_outcome") in {"UP", "DOWN"} for r in rows):
        raise SystemExit("No verified UP/DOWN outcomes. Add official settlements before backtesting.")
    result = run(rows, a.cash, a.threshold, a.entry_window, a.allocation, a.fee_rate)
    Path(a.output).write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({k:v for k,v in result.items() if k != "trades"}, indent=2))
    print(f"Trade log: {a.output}")


if __name__ == "__main__":
    main()
