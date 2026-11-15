"""Telegram-controlled Polymarket paper trader. No wallet or live-order support."""
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from collector import discover, get_book, SESSION, GAMMA
from strategy import choose_side, paper_buy, settle

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
DB_PATH = Path(os.getenv("BOT_DB_PATH", str(DATA / "bot_state.sqlite3")))
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ALLOWED_USER_ID = os.getenv("TELEGRAM_ALLOWED_USER_ID", "").strip()
INITIAL_CASH = float(os.getenv("BOT_INITIAL_CASH", "100"))
DEFAULTS = {"threshold": 0.80, "entry_window": 120, "poll_seconds": 2.0}
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
    with connect() as c:
        c.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (key, json.dumps(value)))
        c.commit()


def cash():
    return float(get("cash"))


def pending():
    with connect() as c:
        return c.execute("SELECT * FROM trades WHERE status='OPEN' ORDER BY id LIMIT 1").fetchone()


def send(chat, text):
    if TG:
        try:
            requests.post(f"{TG}/sendMessage", json={"chat_id": chat, "text": text}, timeout=15).raise_for_status()
        except requests.RequestException as exc:
            print(f"Telegram send failed: {exc}", flush=True)


def notify(text):
    if ALLOWED_USER_ID:
        send(ALLOWED_USER_ID, text)


def outcome_for(market_id):
    r = SESSION.get(f"{GAMMA}/markets/{market_id}", timeout=12)
    r.raise_for_status()
    m = r.json()
    if not m.get("closed"):
        return None
    from collector import as_list
    outcomes, prices = as_list(m.get("outcomes")), as_list(m.get("outcomePrices"))
    if len(outcomes) != 2 or len(prices) != 2:
        return None
    try:
        winners = [str(o).strip().upper() for o, p in zip(outcomes, prices) if float(p) >= .99]
    except (ValueError, TypeError):
        return None
    return winners[0] if len(winners) == 1 and winners[0] in {"UP", "DOWN"} else None


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
    payout = settle(float(pos["shares"]), pos["side"], result)
    pnl = payout - float(pos["cash_spent"])
    new_cash = cash() + payout
    with connect() as c:
        c.execute("UPDATE trades SET resolved_outcome=?,payout=?,pnl=?,cash_after=?,status='SETTLED' WHERE id=? AND status='OPEN'",
                  (result, payout, pnl, new_cash, pos["id"]))
        c.execute("UPDATE settings SET value=? WHERE key='cash'", (json.dumps(new_cash),))
        c.commit()
    notify(f"🏁 SETTLED: {'WIN' if pnl > 0 else 'LOSS'}\n{pos['market_slug']}\nSelected {pos['side']} | Result {result}\nP&L ${pnl:+.2f}\nVirtual balance ${new_cash:.2f}")


def scan():
    if cash() <= 0 or pending():
        return
    now = time.time()
    candidates = []
    for _event, market, up_id, down_id, _start, end in discover(300):
        mid = str(market.get("id"))
        with connect() as c:
            if c.execute("SELECT 1 FROM trades WHERE market_id=?", (mid,)).fetchone():
                continue
        left = end - now
        if not 0 <= left <= int(get("entry_window")):
            continue
        try:
            _ub, ua, _ud = get_book(up_id)
            _db, da, _dd = get_book(down_id)
        except requests.RequestException as exc:
            print(f"Book error: {exc}", flush=True)
            continue
        decision = choose_side(ua, da, float(get("threshold")))
        if decision:
            candidates.append((decision.ask, market, left, decision))
    if not candidates:
        return
    _price, market, left, decision = max(candidates, key=lambda x: x[0])
    fill = paper_buy(cash(), decision.ask, 1.0, 0.0)
    if not fill:
        return
    with connect() as c:
        c.execute("""INSERT INTO trades(market_id,market_slug,question,entry_time,seconds_to_end,side,entry_ask,
          cash_spent,shares,status) VALUES(?,?,?,?,?,?,?,?,?,'OPEN')""",
          (str(market.get("id")), market.get("slug"), market.get("question"),
           datetime.now(timezone.utc).isoformat(), round(left, 2), decision.side,
           decision.ask, fill["cash_spent"], fill["shares"]))
        c.execute("UPDATE settings SET value='0' WHERE key='cash'")
        c.commit()
    notify(f"📝 PAPER TRADE\n{market.get('question') or market.get('slug')}\nSide {decision.side} @ {decision.ask:.3f}\nT-{left:.1f}s\nVirtual stake ${fill['cash_spent']:.2f}\nNo real order placed.")


def engine():
    while not stop_event.is_set():
        try:
            settle_pending()
            if get("active") and not pending():
                scan()
        except Exception as exc:
            print(f"Engine error: {type(exc).__name__}: {exc}", flush=True)
        stop_event.wait(max(.5, float(get("poll_seconds"))))


def stats():
    with connect() as c:
        rows = c.execute("SELECT pnl FROM trades WHERE status='SETTLED'").fetchall()
    pnls = [float(x["pnl"]) for x in rows]
    wins = sum(x > 0 for x in pnls)
    return f"Settled: {len(pnls)} | Wins: {wins} | Losses: {len(pnls)-wins}\nWin rate: {100*wins/len(pnls):.1f}%" if pnls else "No settled trades yet."


def command(chat, user, message):
    if user != ALLOWED_USER_ID:
        return
    parts = message.split()
    cmd = parts[0].split("@")[0].lower() if parts else ""
    args = parts[1:]
    if cmd in ("/start", "/resume"):
        put("active", True); send(chat, "▶️ Paper monitoring ON. No wallet or live orders.")
    elif cmd in ("/stop", "/pause"):
        put("active", False); send(chat, "⏸️ New entries paused. Open position still monitored.")
    elif cmd == "/status":
        p = pending()
        send(chat, f"Monitoring: {'ON' if get('active') else 'OFF'}\nVirtual cash: ${cash():.2f}\nOpen position: {p['side']+' @ '+str(p['entry_ask']) if p else 'None'}")
    elif cmd == "/balance":
        send(chat, f"Virtual balance: ${cash():.2f}" + (" (position open)" if pending() else ""))
    elif cmd == "/stats":
        send(chat, stats())
    elif cmd == "/trades":
        with connect() as c:
            rows = c.execute("SELECT side,entry_ask,status,pnl,market_slug FROM trades ORDER BY id DESC LIMIT 8").fetchall()
        send(chat, "\n".join(f"{r['side']} @{r['entry_ask']:.3f} {r['status']} {r['market_slug']} {'' if r['pnl'] is None else f'P&L {r["pnl"]:+.2f}'}" for r in rows) or "No trades yet.")
    elif cmd == "/settings":
        send(chat, f"Threshold: {float(get('threshold')):.0%}\nEntry window: {get('entry_window')}s\nPoll: {get('poll_seconds')}s\nAllocation: 100% virtual cash\nMode: PAPER ONLY")
    elif cmd == "/set" and len(args) == 2:
        key, raw = args
        try:
            v = float(raw)
            if key == "threshold" and .5 <= v <= .99: put(key, v)
            elif key == "entry_window" and 30 <= v <= 300: put(key, int(v))
            elif key == "poll_seconds" and 1 <= v <= 10: put(key, v)
            else: raise ValueError
            send(chat, f"Saved {key} = {v}")
        except ValueError:
            send(chat, "Invalid setting. threshold .50-.99, entry_window 30-300, poll_seconds 1-10.")
    elif cmd == "/markets":
        try:
            now = time.time()
            found = [(m, end-now) for _e,m,_u,_d,_s,end in discover(300) if 0 <= end-now <= 600]
            found.sort(key=lambda x:x[1])
            send(chat, "\n".join(f"{m.get('slug')} T-{s:.0f}s" for m,s in found[:10]) or "No eligible markets found.")
        except requests.RequestException as exc:
            send(chat, f"Market lookup failed: {exc}")
    else:
        send(chat, "/start /stop /status /balance /trades /stats /markets /settings\n/set threshold 0.82\n/set entry_window 120\n/set poll_seconds 2\n/help")


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
            print(f"Telegram polling error: {exc}", flush=True)
            stop_event.wait(3)


def main():
    if not TOKEN or not ALLOWED_USER_ID:
        raise SystemExit("Configure TELEGRAM_BOT_TOKEN and TELEGRAM_ALLOWED_USER_ID.")
    if INITIAL_CASH <= 0:
        raise SystemExit("BOT_INITIAL_CASH must be positive.")
    with connect() as c:
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
