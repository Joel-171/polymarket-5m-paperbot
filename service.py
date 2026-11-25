"""Telegram-controlled Polymarket paper trader. No wallet or live-order support."""
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

import requests
import paper_session
from collector import as_list, discover_cached, get_asks, read_order_book, token_ids, SESSION, GAMMA
from execution_check import SHARE_STEP, estimate_buy, estimate_profitable_sell, market_fee_rate
from strategy import choose_side

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
DB_PATH = Path(os.getenv("BOT_DB_PATH", str(DATA / "bot_state.sqlite3")))
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ALLOWED_USER_ID = os.getenv("TELEGRAM_ALLOWED_USER_ID", "").strip()
INITIAL_CASH = float(os.getenv("BOT_INITIAL_CASH", "100"))
DEFAULTS = {"threshold": 0.80, "entry_window": 120, "poll_seconds": 10.0}
MIN_QUOTE_INTERVAL = 10
SETTLEMENT_INTERVAL = 30
MAX_BOOK_AGE = 15
PAPER_FILL_DELAY = 0.3
TG = f"https://api.telegram.org/bot{TOKEN}" if TOKEN else ""
stop_event = threading.Event()


def connect():
    DATA.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=20)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
    c.execute("""CREATE TABLE IF NOT EXISTS trades(
      id INTEGER PRIMARY KEY, market_id TEXT UNIQUE NOT NULL, market_slug TEXT, question TEXT,
      entry_time TEXT NOT NULL, seconds_to_end REAL NOT NULL, side TEXT NOT NULL, entry_ask REAL NOT NULL,
      cash_spent REAL NOT NULL, shares REAL NOT NULL, resolved_outcome TEXT, payout REAL, pnl REAL,
      cash_after REAL, status TEXT NOT NULL DEFAULT 'OPEN')""")
    c.execute("""CREATE TABLE IF NOT EXISTS paper_execution(
      trade_id INTEGER PRIMARY KEY REFERENCES trades(id), token_id TEXT NOT NULL,
      book_time_ms INTEGER NOT NULL, book_hash TEXT, available_shares REAL NOT NULL,
      estimated_fee REAL NOT NULL
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS paper_sales(
      id INTEGER PRIMARY KEY, trade_id INTEGER NOT NULL REFERENCES trades(id),
      sold_at TEXT NOT NULL, shares REAL NOT NULL, price REAL NOT NULL,
      fee REAL NOT NULL, net_proceeds REAL NOT NULL, book_key TEXT NOT NULL,
      UNIQUE(trade_id, book_key)
    )""")
    paper_session.initialize(c)
    c.execute("INSERT OR IGNORE INTO settings VALUES('cash',?)", (json.dumps(INITIAL_CASH),))
    for k, v in DEFAULTS.items():
        c.execute("INSERT OR IGNORE INTO settings VALUES(?,?)", (k, json.dumps(v)))
    c.commit()
    return c


@contextmanager
def db():
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get(key):
    with db() as c:
        row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else DEFAULTS.get(key)


def put(key, value):
    with db() as c:
        c.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (key, json.dumps(value)))
        c.commit()


def cash():
    return float(get("cash"))


def pending():
    with db() as c:
        return c.execute("SELECT * FROM trades WHERE status='OPEN' ORDER BY id LIMIT 1").fetchone()


def send(chat, text):
    if TG:
        try:
            requests.post(f"{TG}/sendMessage", json={"chat_id": chat, "text": text}, timeout=15).raise_for_status()
        except requests.RequestException as exc:
            print(f"Telegram send failed: {type(exc).__name__}", flush=True)


def notify(text):
    if ALLOWED_USER_ID:
        send(ALLOWED_USER_ID, text)


def market_for(market_id):
    r = SESSION.get(f"{GAMMA}/markets/{market_id}", timeout=12)
    r.raise_for_status()
    return r.json()


def outcome_for(market_id):
    m = market_for(market_id)
    resolved = m.get("automaticallyResolved") is True or m.get("umaResolutionStatus") == "resolved"
    if not m.get("closed") or not resolved:
        return None
    outcomes = [str(outcome).strip().upper() for outcome in as_list(m.get("outcomes"))]
    if len(outcomes) != 2 or set(outcomes) != {"UP", "DOWN"}:
        return None
    try:
        prices = tuple(float(price) for price in as_list(m.get("outcomePrices")))
    except (ValueError, TypeError):
        return None
    if prices not in ((1.0, 0.0), (0.0, 1.0)):
        return None
    return outcomes[prices.index(1.0)]


def market_close(trade):
    return datetime.fromisoformat(trade["entry_time"]).timestamp() + trade["seconds_to_end"]


def balance_in(conn):
    return Decimal(conn.execute("SELECT value FROM settings WHERE key='cash'").fetchone()[0])


def sales_for(conn, trade_id):
    shares, proceeds = conn.execute(
        "SELECT COALESCE(SUM(shares), 0), COALESCE(SUM(net_proceeds), 0) FROM paper_sales WHERE trade_id=?",
        (trade_id,),
    ).fetchone()
    return Decimal(str(shares)).quantize(SHARE_STEP), Decimal(str(round(proceeds, 6)))


def close_trade(conn, trade, credited_now, total_proceeds, status, outcome=None):
    balance, note = paper_session.transfer(conn, trade["id"], float(balance_in(conn) + credited_now))
    pnl = total_proceeds - Decimal(str(trade["cash_spent"]))
    paper_session.set_cash(conn, balance)
    conn.execute(
        "UPDATE trades SET resolved_outcome=?,payout=?,pnl=?,cash_after=?,status=? WHERE id=?",
        (outcome, float(total_proceeds), float(pnl), balance, status, trade["id"]),
    )
    return pnl, balance, note


def settle_pending():
    pos = pending()
    if not pos:
        return
    try:
        result = outcome_for(pos["market_id"])
    except requests.RequestException as exc:
        print(f"Settlement API error: {exc}", flush=True)
        return
    if result is None:
        return
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        if not c.execute("SELECT 1 FROM trades WHERE id=? AND status='OPEN'", (pos["id"],)).fetchone():
            return
        sold, proceeds = sales_for(c, pos["id"])
        remaining = Decimal(str(pos["shares"])) - sold
        payout = remaining if pos["side"] == result else Decimal(0)
        pnl, new_cash, transfer_note = close_trade(c, pos, payout, proceeds + payout, "SETTLED", result)
    comparison = ""
    if sold:
        sold_cost = sold * Decimal(str(pos["cash_spent"])) / Decimal(str(pos["shares"]))
        hold_proceeds = sold if pos["side"] == result else Decimal(0)
        comparison = f"\nSold shares P&L: ${proceeds - sold_cost:+.2f} | If held: ${hold_proceeds - sold_cost:+.2f}"
    notify(f"🏁 SETTLED: {'WIN' if pos['side'] == result else 'LOSS'}\n{pos['market_slug']}\nSelected {pos['side']} | Result {result}\nTotal trade P&L ${pnl:+.2f}\nVirtual balance ${new_cash:.2f}{transfer_note}{comparison}")


def sell_position():
    pos = pending()
    if pos is None:
        return "There is no open position to sell."
    end = market_close(pos)
    if time.time() >= end:
        return "The market window closed. Waiting for settlement."
    try:
        market = market_for(pos["market_id"])
        up, down = token_ids(market)
        token = up if pos["side"] == "UP" else down
        if market.get("closed") or not market.get("acceptingOrders") or token is None:
            return "The market is no longer accepting orders."
        rate = market_fee_rate(market)
        book = read_order_book(token)
        book_time = int(book["timestamp"]) / 1000
        book_key = book.get("hash") or str(book["timestamp"])
        with db() as c:
            c.execute("BEGIN IMMEDIATE")
            now = time.time()
            if now >= end or not -2 <= now - book_time <= MAX_BOOK_AGE:
                return "The sell quote expired. No paper sale recorded."
            if not c.execute("SELECT 1 FROM trades WHERE id=? AND status='OPEN'", (pos["id"],)).fetchone():
                return "The position has already closed."
            if c.execute(
                "SELECT 1 FROM paper_sales WHERE trade_id=? AND book_key=?", (pos["id"], book_key),
            ).fetchone():
                return "That order-book snapshot was already used. Still holding the remaining shares."
            original_shares = Decimal(str(pos["shares"]))
            cost = Decimal(str(pos["cash_spent"])) / original_shares
            sold, total_proceeds = sales_for(c, pos["id"])
            remaining = original_shares - sold
            sale = estimate_profitable_sell(book, remaining, cost, rate)
            if sale is None:
                return "There isn't enough visible size at a profitable bid after fees. Still holding."
            quantity, price, fee, proceeds = sale
            c.execute(
                "INSERT INTO paper_sales(trade_id,sold_at,shares,price,fee,net_proceeds,book_key) VALUES(?,?,?,?,?,?,?)",
                (pos["id"], datetime.fromtimestamp(now, timezone.utc).isoformat(),
                 float(quantity), float(price), float(fee), float(proceeds), book_key),
            )
            remaining -= quantity
            if remaining == 0:
                _, _, note = close_trade(c, pos, proceeds, total_proceeds + proceeds, "SOLD")
            else:
                paper_session.set_cash(c, balance_in(c) + proceeds)
                note = ""
    except (ValueError, KeyError, InvalidOperation, requests.RequestException) as exc:
        print(f"Paper sell quote failed: {type(exc).__name__}", flush=True)
        return "Could not verify the sell quote. No paper sale recorded."
    return (
        f"Paper sale: {quantity} shares at ${price:.3f}. Fee ${fee:.5f}.\n"
        f"Realized profit on this sale ${proceeds - quantity * cost:+.5f}. "
        f"Still holding {remaining} shares.{note}"
    )


def scan():
    balance = cash()
    if balance <= 0 or pending():
        return
    now = time.time()
    window = int(get("entry_window"))
    if 300 - now % 300 > window:
        return
    threshold = float(get("threshold"))
    with db() as c:
        session = paper_session.latest(c)
        if not paper_session.accepts_entry(c, session, now):
            return
    with db() as c:
        markets = [
            (market, up, down, end)
            for _, market, up, down, _, end in discover_cached()
            if 0 < end - now <= window and not c.execute(
                "SELECT 1 FROM trades WHERE market_id=?", (str(market["id"]),)
            ).fetchone()
        ]
    if not markets:
        return
    asks = get_asks(token for _, up, down, _ in markets for token in (up, down))
    candidates = []
    for market, up, down, end in markets:
        decision = choose_side(asks.get(up), asks.get(down), threshold)
        if decision:
            token = up if decision.side == "UP" else down
            candidates.append((decision.ask, market, end, decision.side, token))
    if not candidates:
        return
    price, market, end, side, token = max(candidates, key=lambda candidate: candidate[0])
    try:
        if stop_event.wait(PAPER_FILL_DELAY):
            return
        book = read_order_book(token)
        book_time = int(book["timestamp"])
        estimate = estimate_buy(
            book, Decimal(str(balance)), Decimal(str(price)), market_fee_rate(market), allow_partial=True,
        )
    except (ValueError, KeyError, InvalidOperation, requests.RequestException) as exc:
        print(f"Execution check failed for {market['slug']}: {exc}", flush=True)
        return
    if not estimate["fits"] or estimate["best_ask"] < Decimal(str(threshold)):
        return
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        now = time.time()
        current = paper_session.latest(c)
        if (current and current["id"]) != (session and session["id"]):
            return
        if not paper_session.accepts_entry(c, current, now):
            return
        if c.execute("SELECT 1 FROM trades WHERE status='OPEN'").fetchone():
            return
        if now >= end or not -2 <= now - book_time / 1000 <= MAX_BOOK_AGE:
            return
        if c.execute("SELECT 1 FROM trades WHERE market_id=?", (str(market.get("id")),)).fetchone():
            return
        if balance != float(c.execute("SELECT value FROM settings WHERE key='cash'").fetchone()[0]):
            return
        left = end - now
        spent = float(estimate["estimated_total_cost"])
        fee = float(estimate["estimated_taker_fee"])
        entry = float(estimate["best_ask"])
        trade = c.execute("""INSERT INTO trades(market_id,market_slug,question,entry_time,seconds_to_end,side,entry_ask,
          cash_spent,shares,status) VALUES(?,?,?,?,?,?,?,?,?,'OPEN')""",
          (str(market.get("id")), market.get("slug"), market.get("question"),
           datetime.fromtimestamp(now, timezone.utc).isoformat(), round(left, 2), side,
           entry, spent, float(estimate["estimated_order_shares"])))
        c.execute(
            "INSERT INTO paper_execution VALUES(?,?,?,?,?,?)",
            (trade.lastrowid, token, book_time, book.get("hash"), float(estimate["available_shares"]), fee),
        )
        paper_session.set_cash(c, estimate["unused_budget"])
    notify(f"📝 PAPER TRADE\n{market.get('question') or market.get('slug')}\nSide {side} @ {entry:.3f}\nT-{left:.1f}s\nFilled {estimate['estimated_order_shares']} of {estimate['desired_shares']} shares\nVirtual cost ${spent:.2f} (est. fee ${fee:.5f})\nCash left ${estimate['unused_budget']:.2f}\nNo real order placed.")


def close_session_if_due():
    now = time.time()
    with db() as c:
        session = paper_session.latest(c)
        if session is None or session["report"] is not None or now < session["ends_at"]:
            return
        c.execute("BEGIN IMMEDIATE")
        report = paper_session.report_if_finished(c, now)
    if report:
        notify(report)


def verify_early_sale(after_id=0):
    with db() as c:
        rows = c.execute(
            """SELECT * FROM trades WHERE status='SOLD' AND resolved_outcome IS NULL
               ORDER BY id <= ?, id""",
            (after_id,),
        ).fetchall()
    now = time.time()
    trade = next((row for row in rows if now >= market_close(row)), None)
    if trade is None:
        return after_id
    try:
        outcome = outcome_for(trade["market_id"])
    except requests.RequestException as exc:
        print(f"Paper sale resolution lookup failed: {type(exc).__name__}", flush=True)
        return trade["id"]
    if outcome is None:
        return trade["id"]
    with db() as c:
        updated = c.execute(
            "UPDATE trades SET resolved_outcome=? WHERE id=? AND status='SOLD' AND resolved_outcome IS NULL",
            (outcome, trade["id"]),
        ).rowcount
    if updated:
        hold_pnl = (trade["shares"] if outcome == trade["side"] else 0) - trade["cash_spent"]
        notify(
            f"Official result after paper sale: {trade['market_slug']} → {outcome}\n"
            f"Net P&L selling: ${trade['pnl']:+.2f} | Holding: ${hold_pnl:+.2f}"
        )
    return trade["id"]


def quote_interval():
    return max(MIN_QUOTE_INTERVAL, float(get("poll_seconds")))


def engine():
    next_scan = next_settlement = next_sale_check = 0
    last_sale_checked = 0
    while not stop_event.is_set():
        try:
            now = time.monotonic()
            position = pending()
            if position and now >= next_settlement:
                if time.time() >= market_close(position):
                    next_settlement = now + SETTLEMENT_INTERVAL
                    settle_pending()
            close_session_if_due()
            if not position and now >= next_scan and get("active"):
                next_scan = now + quote_interval()
                scan()
            if now >= next_sale_check:
                next_sale_check = now + SETTLEMENT_INTERVAL
                last_sale_checked = verify_early_sale(last_sale_checked)
        except Exception as exc:
            print(f"Engine error: {type(exc).__name__}: {exc}", flush=True)
        stop_event.wait(1)


def stats():
    with db() as c:
        total, wins = c.execute(
            "SELECT COUNT(resolved_outcome), COUNT(CASE WHEN resolved_outcome=side THEN 1 END) FROM trades"
        ).fetchone()
    return f"Resolved: {total} | Wins: {wins} | Losses: {total-wins}\nWin rate: {wins/total:.1%}" if total else "No settled trades yet."


def command(chat, user, message):
    if user != ALLOWED_USER_ID:
        return
    parts = message.split()
    cmd = parts[0].split("@")[0].lower() if parts else ""
    args = parts[1:]
    if cmd == "/overnight":
        if len(args) != 1 or len(args[0]) > 100:
            send(chat, "Usage: /overnight <virtual-wallet-label>. Example: /overnight vault")
            return
        with db() as c:
            c.execute("BEGIN IMMEDIATE")
            reply = paper_session.start(c, args[0], time.time())
        send(chat, reply)
    elif cmd in ("/start", "/resume"):
        with db() as c:
            c.execute("BEGIN IMMEDIATE")
            session = paper_session.latest(c)
            if session and time.time() >= session["ends_at"]:
                reply = "The entry window ended. Use /report or start a new /overnight session."
            else:
                paper_session.set_enabled(c, True)
                reply = "▶️ Paper monitoring ON."
        send(chat, reply)
    elif cmd in ("/stop", "/pause"):
        put("active", False); send(chat, "⏸️ New entries paused. Open position still monitored.")
    elif cmd == "/endsession":
        with db() as c:
            c.execute("BEGIN IMMEDIATE")
            reply = paper_session.end(c, time.time())
        send(chat, reply)
    elif cmd == "/sell":
        send(chat, sell_position())
    elif cmd == "/status":
        p = pending()
        with db() as c:
            session = paper_session.latest(c)
        deadline = f"\nOvernight entry cutoff: {paper_session.utc_label(session['ends_at'])}" if session else ""
        active = get("active") and (session is None or time.time() < session["ends_at"])
        send(chat, f"Monitoring: {'ON' if active else 'OFF'}\nVirtual cash: ${cash():.2f}\nOpen position: {p['side']+' @ '+str(p['entry_ask']) if p else 'None'}{deadline}")
    elif cmd == "/wallet":
        with db() as c:
            reply = paper_session.wallet_summary(c)
        send(chat, reply)
    elif cmd == "/report":
        with db() as c:
            session = paper_session.latest(c)
            provisional = paper_session.timeout_report(c, session["id"]) if session else None
        if session is None:
            reply = "No overnight session yet. Use /overnight <virtual-wallet-label>."
        elif session["report"] is not None:
            reply = session["report"]
        elif provisional:
            reply = provisional
        elif time.time() >= session["ends_at"]:
            reply = "Entry window closed. Final report is pending settlement or the next bot check."
        else:
            reply = f"Overnight session in progress. Entries close at {paper_session.utc_label(session['ends_at'])}."
        send(chat, reply)
    elif cmd == "/balance":
        send(chat, f"Virtual balance: ${cash():.2f}" + (" (position open)" if pending() else ""))
    elif cmd == "/stats":
        send(chat, stats())
    elif cmd == "/trades":
        with db() as c:
            rows = c.execute("SELECT side,entry_ask,status,pnl,market_slug,estimated_fee FROM trades LEFT JOIN paper_execution ON trade_id=id ORDER BY id DESC LIMIT 8").fetchall()
        send(chat, "\n".join(
            f"{r['side']} @{r['entry_ask']:.3f} {r['status']} {r['market_slug']}"
            + (f" P&L {r['pnl']:+.2f}" if r["pnl"] is not None else "")
            + (f" | Fee ${r['estimated_fee']:.5f}" if r["estimated_fee"] is not None else "")
            for r in rows
        ) or "No trades yet.")
    elif cmd == "/settings":
        send(chat, f"Threshold: {float(get('threshold')):.0%}\nEntry window: {get('entry_window')}s\nQuote poll: {quote_interval():g}s\nAllocation: up to 100% virtual cash including estimated fee\nMode: PAPER ONLY")
    elif cmd == "/set" and len(args) == 2:
        key, raw = args
        try:
            v = float(raw)
            if key == "threshold" and .5 <= v <= .99: put(key, v)
            elif key == "entry_window" and 30 <= v <= 300: put(key, int(v))
            elif key == "poll_seconds" and 10 <= v <= 60: put(key, v)
            else: raise ValueError
            send(chat, f"Saved {key} = {v}")
        except ValueError:
            send(chat, "Invalid setting. threshold .50-.99, entry_window 30-300, poll_seconds 10-60.")
    elif cmd == "/markets":
        try:
            now = time.time()
            found = [(m, end-now) for _e,m,_u,_d,_s,end in discover_cached() if 0 <= end-now <= 600]
            found.sort(key=lambda x:x[1])
            send(chat, "\n".join(f"{m.get('slug')} T-{s:.0f}s" for m,s in found[:10]) or "No eligible markets found.")
        except requests.RequestException as exc:
            send(chat, f"Market lookup failed: {exc}")
    else:
        send(chat, "/overnight <virtual-wallet-label> /endsession /wallet /report\n/start /stop /sell /status /balance /trades /stats /markets /settings\n/set threshold 0.82\n/set entry_window 120\n/set poll_seconds 10\n/help")


def telegram():
    offset = None
    while not stop_event.is_set():
        try:
            params = {"timeout": 20}
            if offset is not None: params["offset"] = offset
            r = requests.get(f"{TG}/getUpdates", params=params, timeout=30)
            r.raise_for_status()
            for update in r.json().get("result", []):
                offset = update["update_id"] + 1
                msg = update.get("message") or update.get("edited_message")
                if msg and msg.get("text"):
                    command(str(msg["chat"]["id"]), str(msg.get("from", {}).get("id", "")), msg["text"])
        except requests.RequestException as exc:
            print(f"Telegram polling error: {type(exc).__name__}", flush=True)
            stop_event.wait(3)


def main():
    if not TOKEN or not ALLOWED_USER_ID:
        raise SystemExit("Configure TELEGRAM_BOT_TOKEN and TELEGRAM_ALLOWED_USER_ID.")
    if INITIAL_CASH <= 0:
        raise SystemExit("BOT_INITIAL_CASH must be positive.")
    with db() as c:
        c.execute("INSERT OR IGNORE INTO settings VALUES('active','false')")
        c.commit()
    print("PAPER-ONLY bot started. Send /status or /start in Telegram.", flush=True)
    threading.Thread(target=engine, daemon=True).start()
    try:
        telegram()
    except KeyboardInterrupt:
        stop_event.set()


if __name__ == "__main__":
    main()
