"""Read-only Gamma/CLOB market discovery and order-book access."""
import json
from datetime import datetime
import requests

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
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


def candidate_text(event, market):
    text = " ".join(str(x or "") for x in (
        event.get("title"), event.get("slug"), market.get("question"), market.get("slug")
    )).lower()
    crypto = any(x in text for x in ("bitcoin", "btc", "ethereum", "eth", "solana", "sol", "xrp"))
    direction = any(x in text for x in ("up or down", "updown", "up-down"))
    duration = any(x in text for x in ("5m", "5-min", "5 min", "five minute"))
    return crypto and direction and duration


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


def discover(limit=300):
    r = SESSION.get(f"{GAMMA}/events", params={"active": "true", "closed": "false", "limit": limit}, timeout=20)
    r.raise_for_status()
    payload = r.json()
    events = payload if isinstance(payload, list) else payload.get("events", [])
    found = []
    for event in events:
        for market in event.get("markets") or []:
            if not candidate_text(event, market):
                continue
            up, down = token_ids(market)
            end = timestamp(market.get("endDate") or event.get("endDate"))
            start = timestamp(market.get("startDate") or event.get("startDate"))
            if up and down and end:
                found.append((event, market, up, down, start, end))
    return found
