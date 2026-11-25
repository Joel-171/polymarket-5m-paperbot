"""Timed paper sessions and their virtual transfer ledger."""
from datetime import datetime, timezone
from math import ceil

STARTING_CASH = 10.0
CASH_OUT_TARGET = 10_000.0
ENTRY_INTERVAL = 300
DURATION = 6 * 60 * 60
SETTLEMENT_TIMEOUT = 30 * 60


def initialize(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS paper_sessions(
        id INTEGER PRIMARY KEY, starts_at REAL NOT NULL, ends_at REAL NOT NULL,
        wallet TEXT NOT NULL, after_trade_id INTEGER NOT NULL, report TEXT
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS paper_transfers(
        trade_id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL,
        created_at TEXT NOT NULL, amount REAL NOT NULL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS paper_session_timeouts(
        session_id INTEGER PRIMARY KEY, report TEXT NOT NULL
    )""")


def latest(conn):
    return conn.execute("SELECT * FROM paper_sessions ORDER BY id DESC LIMIT 1").fetchone()


def enabled(conn):
    row = conn.execute("SELECT value FROM settings WHERE key='active'").fetchone()
    return row is not None and row[0] == "true"


def set_enabled(conn, active):
    conn.execute(
        "INSERT OR REPLACE INTO settings VALUES('active', ?)",
        ("true" if active else "false",),
    )


def set_cash(conn, balance):
    conn.execute("UPDATE settings SET value=? WHERE key='cash'", (str(balance),))


def start(conn, wallet, now):
    session = latest(conn)
    if session and session["report"] is None:
        return "An overnight session is already open. Check /status or /report."
    if conn.execute("SELECT 1 FROM trades WHERE status='OPEN'").fetchone():
        return "Wait for the open trade to settle before starting an overnight session."
    conn.execute(
        """INSERT INTO paper_sessions(starts_at, ends_at, wallet, after_trade_id)
           SELECT ?, ?, ?, COALESCE(MAX(id), 0) FROM trades""",
        (now, now + DURATION, wallet),
    )
    set_cash(conn, STARTING_CASH)
    set_enabled(conn, True)
    return (
        f"Six-hour paper session started with ${STARTING_CASH:.2f}.\n"
        f"Virtual wallet: {wallet}\nEntries close at {utc_label(now + DURATION)}."
    )


def utc_label(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%d %b %H:%M UTC")


def end(conn, now):
    session = latest(conn)
    if session is None:
        return "No overnight session yet."
    if session["report"] is not None:
        return session["report"]
    set_enabled(conn, False)
    conn.execute(
        "UPDATE paper_sessions SET ends_at=MIN(ends_at, ?) WHERE id=?", (now, session["id"]),
    )
    return report_if_finished(conn, now) or "Entries ended. The report will follow when the open position settles."


def timeout_report(conn, session_id):
    row = conn.execute("SELECT report FROM paper_session_timeouts WHERE session_id=?", (session_id,)).fetchone()
    return row[0] if row else None


def accepts_entry(conn, session, now):
    if not enabled(conn):
        return False
    if session is None:
        return True
    if session["report"] is not None or not session["starts_at"] <= now < session["ends_at"]:
        return False
    last_trade = conn.execute(
        "SELECT entry_time FROM trades WHERE id > ? ORDER BY id DESC LIMIT 1",
        (session["after_trade_id"],),
    ).fetchone()
    if last_trade is None:
        return True
    last_entry = datetime.fromisoformat(last_trade["entry_time"]).timestamp()
    return now // ENTRY_INTERVAL > last_entry // ENTRY_INTERVAL and (now - session["starts_at"]) // ENTRY_INTERVAL > (
        last_entry - session["starts_at"]
    ) // ENTRY_INTERVAL


def transfer(conn, trade_id, balance):
    session = latest(conn)
    if (
        session is None
        or session["report"] is not None
        or trade_id <= session["after_trade_id"]
        or balance < CASH_OUT_TARGET
    ):
        return balance, ""
    amount = balance - STARTING_CASH
    conn.execute(
        "INSERT INTO paper_transfers VALUES(?, ?, ?, ?)",
        (trade_id, session["id"], datetime.now(timezone.utc).isoformat(), amount),
    )
    return STARTING_CASH, f"\nSimulated transfer ${amount:,.2f} to {session['wallet']}."


def report_if_finished(conn, now):
    session = latest(conn)
    if session is None or session["report"] is not None or now < session["ends_at"]:
        return None
    set_enabled(conn, False)
    entered, open_count, resolved, wins, resolved_shares, won_shares, resolved_cost = conn.execute(
        """SELECT COUNT(*), COUNT(CASE WHEN status='OPEN' THEN 1 END),
                  COUNT(resolved_outcome), COUNT(CASE WHEN resolved_outcome=side THEN 1 END),
                  COALESCE(SUM(CASE WHEN resolved_outcome IS NOT NULL THEN shares END), 0),
                  COALESCE(SUM(CASE WHEN resolved_outcome=side THEN shares END), 0),
                  COALESCE(SUM(CASE WHEN resolved_outcome IS NOT NULL THEN cash_spent END), 0)
           FROM trades WHERE id > ?""",
        (session["after_trade_id"],),
    ).fetchone()
    if open_count and (now < session["ends_at"] + SETTLEMENT_TIMEOUT or timeout_report(conn, session["id"])):
        return None
    transfers, amount = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(amount), 0) FROM paper_transfers WHERE session_id=?",
        (session["id"],),
    ).fetchone()
    balance = float(conn.execute("SELECT value FROM settings WHERE key='cash'").fetchone()[0])
    sold_profit, sell_fees, open_cost, buy_fees = conn.execute(
        """SELECT COALESCE(SUM(sale_proceeds - sold_size * cash_spent / shares), 0),
                  COALESCE(SUM(sale_fees), 0),
                  COALESCE(SUM(CASE WHEN status='OPEN' THEN cash_spent * (1 - COALESCE(sold_size, 0) / shares) END), 0),
                  COALESCE(SUM(estimated_fee), 0)
           FROM trades LEFT JOIN paper_execution ON trade_id=id
           LEFT JOIN (
               SELECT trade_id AS sold_trade, SUM(net_proceeds) AS sale_proceeds,
                      SUM(shares) AS sold_size, SUM(fee) AS sale_fees
               FROM paper_sales GROUP BY trade_id
           ) ON sold_trade=id
           WHERE id > ?""",
        (session["after_trade_id"],),
    ).fetchone()
    if resolved_shares:
        edge_line = (
            f"Entry win rate by shares: {won_shares / resolved_shares:.1%} vs "
            f"{resolved_cost / resolved_shares:.1%} break-even incl. entry fees "
            f"({resolved} officially resolved entries)\n"
        )
    else:
        edge_line = "Entry win rate and break-even: awaiting resolved entries.\n"
    duration = max(0, min(DURATION, session["ends_at"] - session["starts_at"]))
    heading = "Settlement pending after 30 minutes. Provisional report." if open_count else (
        "Six-hour paper session complete." if duration == DURATION else "Paper session ended."
    )
    total = balance + amount + open_cost
    report = (
        f"{heading}\n"
        f"Trades: {entered} | Wins: {wins} | Losses: {resolved - wins}\n"
        + edge_line
        + f"Five-minute windows without an entry: {max(0, ceil(duration / ENTRY_INTERVAL) - entered)}\n"
        f"Estimated fees: ${buy_fees + sell_fees:,.5f} | Early-sale P&L: ${sold_profit:+,.2f}\n"
        f"Simulated cash-outs: {transfers} | ${amount:,.2f} to {session['wallet']}\n"
        f"Remaining virtual cash: ${balance:,.2f}\n"
        + (f"Unsettled position at cost: ${open_cost:,.2f}\n" if open_count else "")
        + f"Total including cash-outs: ${total:,.2f} | Net P&L: ${total - STARTING_CASH:+,.2f}"
    )
    if open_count:
        conn.execute("INSERT INTO paper_session_timeouts VALUES(?,?)", (session["id"], report))
    else:
        conn.execute("UPDATE paper_sessions SET report=? WHERE id=?", (report, session["id"]))
    return report


def wallet_summary(conn):
    wallets = conn.execute(
        """SELECT s.wallet, COUNT(t.trade_id), COALESCE(SUM(t.amount), 0)
           FROM paper_sessions s LEFT JOIN paper_transfers t ON t.session_id=s.id
           GROUP BY s.wallet ORDER BY MAX(s.id) DESC LIMIT 10"""
    ).fetchall()
    return "\n".join(
        f"{wallet}: ${amount:,.2f} ({count} transfers)" for wallet, count, amount in wallets
    ) or "No virtual wallets yet."
