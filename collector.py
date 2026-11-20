"""Read-only Gamma/CLOB market discovery and order-book access."""
import json
import time
from datetime import datetime
import requests

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
FIVE_MINUTE_SYMBOLS = ("btc", "eth", "sol", "xrp")
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "polymarket-paper-research/1.0"})


def as_list(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return []
    return value if isinstance(value, list) else []


def timestamp(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return None


def token_ids(market):
    outcomes, ids = as_list(market.get("outcomes")), as_list(market.get("clobTokenIds"))
    if len(outcomes) != 2 or len(ids) != 2:
        return None, None
    mapping = {str(o).strip().lower(): str(t) for o, t in zip(outcomes, ids)}
    return mapping.get("up"), mapping.get("down")


def get_book(token_id):
    r = SESSION.get(f"{CLOB}/book", params={"token_id": token_id}, timeout=12)
    r.raise_for_status()
    book = r.json()
    bids = [(float(x["price"]), float(x["size"])) for x in book.get("bids", []) if float(x.get("size", 0)) > 0]
    asks = [(float(x["price"]), float(x["size"])) for x in book.get("asks", []) if float(x.get("size", 0)) > 0]
    bid = max((p for p, _ in bids), default=None)
    ask = min((p for p, _ in asks), default=None)
    depth = sum(p * size for p, size in asks if ask is not None and p <= ask + 0.01)
    return bid, ask, depth


def discover():
    slot_start = int(time.time()) // 300 * 300
    found = []
    for symbol in FIVE_MINUTE_SYMBOLS:
        slug = f"{symbol}-updown-5m-{slot_start}"
        r = SESSION.get(f"{GAMMA}/events/slug/{slug}", timeout=12)
        if r.status_code == 404:
            continue
        r.raise_for_status()
        event = r.json()
        for market in event.get("markets") or []:
            if market.get("slug") != slug or market.get("closed") or not market.get("acceptingOrders"):
                continue
            up, down = token_ids(market)
            end = timestamp(market.get("endDate") or event.get("endDate"))
            if up and down and end == slot_start + 300:
                found.append((event, market, up, down, slot_start, end))
    return found
