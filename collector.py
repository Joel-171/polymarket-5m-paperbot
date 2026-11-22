"""Read-only Gamma/CLOB market discovery and order-book access."""
import json
import threading
import time
from datetime import datetime
import requests

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
FIVE_MINUTE_SYMBOLS = ("btc", "eth", "sol", "xrp")
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "polymarket-paper-research/1.0"})
_market_cache_lock = threading.Lock()
_market_cache = {}


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


def read_order_book(token_id):
    r = SESSION.get(f"{CLOB}/book", params={"token_id": token_id}, timeout=12)
    r.raise_for_status()
    return r.json()


def get_book(token_id):
    book = read_order_book(token_id)
    bids = [(float(x["price"]), float(x["size"])) for x in book.get("bids", []) if float(x.get("size", 0)) > 0]
    asks = [(float(x["price"]), float(x["size"])) for x in book.get("asks", []) if float(x.get("size", 0)) > 0]
    bid = max((p for p, _ in bids), default=None)
    ask = min((p for p, _ in asks), default=None)
    depth = sum(p * size for p, size in asks if ask is not None and p <= ask + 0.01)
    return bid, ask, depth


def get_asks(token_ids):
    # CLOB's SELL quote is the lowest resting sell price, or best ask.
    payload = [{"token_id": token, "side": "SELL"} for token in dict.fromkeys(token_ids)]
    response = SESSION.post(f"{CLOB}/prices", json=payload, timeout=12)
    response.raise_for_status()
    return {
        token: float(sides["SELL"])
        for token, sides in response.json().items()
        if sides.get("SELL") is not None
    }


def discover_cached():
    slot = int(time.time()) // 300
    with _market_cache_lock:
        now = time.monotonic()
        if _market_cache.get("slot") == slot and now < _market_cache["refresh_at"]:
            return _market_cache["markets"]
        markets = [market for market in discover() if market[-1] == (slot + 1) * 300]
        # Retry incomplete discovery instead of caching missing listings all window.
        ttl = 300 if len(markets) == len(FIVE_MINUTE_SYMBOLS) else 30
        _market_cache.update(slot=slot, refresh_at=now + ttl, markets=markets)
        return markets


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
