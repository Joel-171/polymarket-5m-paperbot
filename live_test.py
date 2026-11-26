"""Run one manually approved Polymarket buy and track its payout."""
import argparse
import getpass
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_DOWN

import requests

from collector import discover_cached, get_asks, read_order_book
from execution_check import SHARE_STEP, market_fee_rate, taker_fee
from service import DATA, DEFAULTS, MAX_BOOK_AGE, SETTLEMENT_INTERVAL, outcome_for
from strategy import choose_side

CENT = Decimal("0.01")
MIN_MARKET_NOTIONAL = Decimal("1")
UNIT_SCALE = Decimal("1000000")
JOURNAL = DATA / "live-test.jsonl"


@dataclass(frozen=True)
class Quote:
    market_id: str
    slug: str
    condition_id: str
    token: str
    side: str
    end: float
    price: Decimal
    amount: Decimal
    estimated_shares: Decimal
    estimated_fee: Decimal
    max_spend: Decimal
    fee_rate: Decimal


def check_location():
    response = requests.get("https://polymarket.com/api/geoblock", timeout=10)
    response.raise_for_status()
    result = response.json()
    country = result.get("country")
    if result.get("blocked") is not False or not country:
        raise SystemExit("Polymarket geo check did not permit orders on this connection.")
    print(f"Polymarket geo check: allowed ({country}).", flush=True)


def book_quote(market_id, slug, condition_id, token, side, end, limit, budget, fee_rate):
    book = read_order_book(token, fresh=True)
    now = time.time()
    if str(book.get("asset_id")) != token or not 0 < end - now <= DEFAULTS["entry_window"]:
        return None
    if not -2 <= now - int(book["timestamp"]) / 1000 <= MAX_BOOK_AGE:
        return None

    asks = [(Decimal(row["price"]), Decimal(row["size"])) for row in book["asks"]]
    valid = [(price, size) for price, size in asks if 0 < price < 1 and size > 0]
    if not valid:
        return None
    price = min(price for price, _ in valid)
    if not Decimal(str(DEFAULTS["threshold"])) <= price <= limit:
        return None

    minimum = Decimal(book["min_order_size"])
    if minimum <= 0:
        return None
    amount = max(MIN_MARKET_NOTIONAL, minimum * price).quantize(CENT, rounding=ROUND_CEILING)
    shares = (amount / price).quantize(SHARE_STEP, rounding=ROUND_DOWN)
    if shares < minimum:
        amount += CENT
        shares = (amount / price).quantize(SHARE_STEP, rounding=ROUND_DOWN)
    depth = sum(size for ask, size in valid if ask == price)
    if shares < minimum or depth < shares:
        return None

    fee = taker_fee(shares, price, fee_rate)
    max_spend = (amount + fee).quantize(CENT, rounding=ROUND_CEILING)
    if max_spend > budget:
        return None
    return Quote(
        market_id, slug, condition_id, token, side, end, price, amount, shares, fee,
        max_spend, fee_rate,
    )


def find_quote(budget):
    now = time.time()
    if 300 - now % 300 > DEFAULTS["entry_window"]:
        return None
    markets = discover_cached()
    if not markets:
        return None
    asks = get_asks(token for _, _, up, down, _, _ in markets for token in (up, down))
    ranked = []
    for _, market, up, down, _, end in markets:
        decision = choose_side(asks.get(up), asks.get(down), DEFAULTS["threshold"])
        if decision:
            token = up if decision.side == "UP" else down
            ranked.append((decision.ask, market, token, decision.side, end))
    if not ranked:
        return None
    price, market, token, side, end = max(ranked, key=lambda row: row[0])
    return book_quote(
        str(market["id"]), market["slug"], market["conditionId"], token, side,
        end, Decimal(str(price)), budget, market_fee_rate(market),
    )


def wait_until(fetch, *, seconds, interval):
    deadline = time.monotonic() + seconds
    while True:
        result = fetch()
        if result is not None:
            return result
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        time.sleep(min(remaining, interval))


def collateral(client):
    return Decimal(client.get_balance_allowance(asset_type="COLLATERAL").balance) / UNIT_SCALE


def confirmed_fills(client, quote, response):
    page = client.list_account_trades(asset_id=quote.token, market=quote.condition_id).first_page()
    fills = [trade for trade in page.items if trade.taker_order_id == response.order_id]
    if any(fill.status not in ("CONFIRMED", "FAILED") for fill in fills):
        return None
    known_ids = {fill.id for fill in fills}
    if not set(response.trade_ids).issubset(known_ids):
        return None
    order = client.get_order(order_id=response.order_id)
    if order.status.upper() not in ("MATCHED", "CANCELED", "CANCELLED", "UNMATCHED"):
        return None
    settled = [fill for fill in fills if fill.status == "CONFIRMED"]
    if sum(fill.size for fill in fills) != order.size_matched:
        return None
    return settled


def check_winner(quote):
    try:
        return outcome_for(quote.market_id, quote.slug)
    except requests.RequestException:
        return None


def record(journal, event, **fields):
    row = {"utc": datetime.now(timezone.utc).isoformat(), "event": event, **fields}
    print(json.dumps(row, default=str), file=journal, flush=True)
    os.fsync(journal.fileno())
    print(json.dumps(row, default=str), flush=True)


def run_order(client, quote, before_buy, journal):
    record(journal, "submitting", **vars(quote), collateral_before_buy=before_buy)
    if time.time() >= quote.end:
        record(journal, "expired_before_submit")
        return
    sent_at = time.monotonic()
    response = client.place_market_order(
        asset_id=quote.token, side="BUY", amount=quote.amount, max_spend=quote.max_spend,
        max_price=quote.price, order_type="FAK",
    )
    if not response.ok:
        record(journal, "rejected", code=response.code)
        return

    record(journal, "accepted", order_id=response.order_id, status=response.status)
    fills = wait_until(lambda: confirmed_fills(client, quote, response), seconds=120, interval=3)
    if fills is None:
        record(journal, "fill_unconfirmed", order_id=response.order_id)
        return
    if not fills:
        record(journal, "no_confirmed_fill", order_id=response.order_id)
        return

    shares = sum(fill.size for fill in fills)
    notional = sum(fill.size * fill.price for fill in fills)

    def fill_balance():
        available = collateral(client)
        return available if before_buy - available >= notional else None

    after_buy = wait_until(fill_balance, seconds=15, interval=3)
    # The dedicated wallet must have no other activity during the test.
    observed_fee = before_buy - after_buy - notional if after_buy is not None else None
    record(
        journal, "fill_confirmed", order_id=response.order_id,
        seconds_since_submit=round(time.monotonic() - sent_at, 2),
        fills=[{"id": f.id, "price": f.price, "shares": f.size, "fee_rate_bps": f.fee_rate_bps} for f in fills],
        notional=notional, collateral_after_buy=after_buy, observed_fee=observed_fee,
    )

    time.sleep(max(0, quote.end - time.time()))
    winner = wait_until(
        lambda: check_winner(quote), seconds=1800,
        interval=SETTLEMENT_INTERVAL,
    )
    if winner is None:
        record(journal, "resolution_unconfirmed")
        return
    resolved_at = time.monotonic()
    record(journal, "resolution_seen", winner=winner, seconds_since_window_end=round(time.time() - quote.end, 2))
    if winner != quote.side:
        return

    before_redeem = collateral(client)
    record(journal, "redeeming", collateral_before_redeem=before_redeem)
    handle = client.redeem_positions(condition_id=quote.condition_id)
    record(journal, "redemption_submitted", tx_id=handle.transaction_id, tx_hash=handle.transaction_hash)
    receipt = handle.wait()
    record(journal, "redemption_confirmed", tx_hash=receipt.transaction_hash)

    def credited():
        available = collateral(client)
        return available if available >= before_redeem + shares else None

    available = wait_until(credited, seconds=120, interval=3)
    if available is None:
        record(journal, "payout_balance_unconfirmed")
    else:
        record(
            journal, "payout_spendable", collateral=available,
            seconds_since_resolution_seen=round(time.monotonic() - resolved_at, 2),
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cap", required=True, help="Total test wallet budget in pUSD.")
    parser.add_argument("--execute", action="store_true", help="Enable the approval prompt for one live order.")
    args = parser.parse_args()
    try:
        cap = Decimal(args.cap)
        if not cap.is_finite() or not 0 < cap <= 10 or cap != cap.quantize(CENT):
            raise ValueError
    except (InvalidOperation, ValueError):
        parser.error("--cap must be a positive amount up to 10 pUSD, with at most two decimals.")

    check_location()
    if not args.execute:
        quote = wait_until(lambda: find_quote(cap), seconds=300, interval=10)
        print(json.dumps(vars(quote), indent=2, default=str) if quote else "No qualifying quote within five minutes.")
        return
    if JOURNAL.exists():
        raise SystemExit(f"A test already attempted an order. Reconcile {JOURNAL} before another test.")

    from polymarket import RelayerApiKey, SecureClient

    wallet = input("Dedicated Polymarket trading wallet address: ").strip()
    if not wallet:
        raise SystemExit("Enter the trading wallet address shown by Polymarket.")
    key = os.getenv("POLYMARKET_PRIVATE_KEY") or getpass.getpass("Dedicated wallet signer key: ")
    relayer_key = os.getenv("POLYMARKET_RELAYER_API_KEY") or getpass.getpass("Relayer API key: ")
    relayer_address = os.getenv("POLYMARKET_RELAYER_API_KEY_ADDRESS") or input("Relayer key address: ").strip()
    if not key or not relayer_key or not relayer_address:
        raise SystemExit("The signer key, relayer key, and relayer key address are required.")
    api_key = RelayerApiKey(key=relayer_key, address=relayer_address)
    with SecureClient.create(private_key=key, wallet=wallet, api_key=api_key) as client:
        balance = collateral(client)
        if not 0 < balance <= cap:
            raise SystemExit(f"Dedicated wallet collateral must be positive and at or below {cap} pUSD.")
        if client.get_closed_only_mode():
            raise SystemExit("Polymarket reports this account is in close-only mode.")
        open_orders = client.list_wallet_open_orders if client.wallet_type == "DEPOSIT_WALLET" else client.list_open_orders
        if open_orders().first_page().items or client.list_positions().first_page().items:
            raise SystemExit("Use a dedicated wallet with no open orders or existing positions.")
        print("Waiting up to five minutes for an entry.", flush=True)
        quote = wait_until(lambda: find_quote(balance), seconds=300, interval=10)
        if quote is None:
            raise SystemExit("No qualifying order fits your wallet balance.")
        print(json.dumps(vars(quote), indent=2, default=str), flush=True)
        if input(f"Approve this one live buy, estimated maximum {quote.max_spend} pUSD? Type BUY: ").strip() != "BUY":
            return
        fresh = book_quote(
            quote.market_id, quote.slug, quote.condition_id, quote.token, quote.side,
            quote.end, quote.price, quote.max_spend, quote.fee_rate,
        )
        if fresh is None or collateral(client) != balance or time.time() >= quote.end:
            raise SystemExit("The quote expired, the price moved above the approved price, or the wallet balance changed.")
        JOURNAL.parent.mkdir(parents=True, exist_ok=True)
        with JOURNAL.open("x", encoding="utf-8") as journal:
            record(journal, "approved", **vars(quote))
            run_order(client, fresh, balance, journal)


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        raise SystemExit("Test interrupted. Check the journal and your Polymarket account before retrying.")
    except Exception as error:
        raise SystemExit(
            f"Test stopped ({type(error).__name__}). Check the journal and your Polymarket account before retrying."
        ) from None
