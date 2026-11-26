# Polymarket 5-Minute Telegram Paper-Trading Bot

Paper-only research software. Telegram controls monitoring and reports. The engine reads public Polymarket Gamma/CLOB data, simulates positions and records outcomes. It has no wallet, private keys, signing or live-order endpoint.

## Strategy defaults
- Looks up current BTC, ETH, SOL, and XRP five-minute UP/DOWN markets.
- Entry during final 120 seconds.
- Trigger: best executable ask >= 0.80.
- Choose the highest qualifying ask among discovered eligible markets.
- Budget up to 100% of virtual cash including estimated taker fees. Use visible
  shares at the best ask, up to the price selected by the quote scan, and keep
  unspent cash idle while the position is open. Skip old or subminimum fills.
- Hold until the public market is resolved at $1 for one outcome. Use
  `/sell` for an early paper sale only when the visible bid can cover the
  allocated entry cost and exit fee.

An 80-cent contract is not proof of an 80% true win probability. A losing all-in position can wipe out the virtual bankroll. This is a research setting, not a recommendation.

## Telegram commands
/start resume monitoring; /stop pause new entries; /status; /balance; /trades; /stats; /markets; /settings; /help.
`/endsession` closes new entries and requests the report. `/sell` checks the
open position for a profitable simulated early exit.
Change settings with /set threshold 0.82, /set entry_window 120, or /set poll_seconds 10.
Only TELEGRAM_ALLOWED_USER_ID can control the bot.

Quote checks start in the entry window. Market listings are cached, there is
one compact price request per scan, and only the selected outcome gets a
full order-book request after a modeled 0.3-second delay. Quote polling is clamped to at least 10 seconds
(set 10–60 with `/set poll_seconds`). Pending trades are checked for
settlement at most once per 30 seconds after close.

## Six-hour paper session

Run the bot on a computer with Python 3.12 or newer. Install the dependencies
with `python -m pip install -r requirements.txt`, set
`TELEGRAM_BOT_TOKEN` and `TELEGRAM_ALLOWED_USER_ID` in the process environment,
and run `python service.py`. The service does not load `.env.example`.
Use `/status` in Telegram to confirm it responds.

- At 22:00 Ghana time (UTC), send `/overnight vault` to start a fresh $10
  paper session. Replace `vault` with any single-word label or public address.
  No wallet connection or private key is needed. Keep the computer awake and online.
- The session follows the current paper strategy settings, with one entry
  per five-minute session window at most. When settled cash reaches $10,000,
  the bot logs a simulated transfer of everything above $10 to that label.
  It keeps $10 in play. A loss can exhaust the bankroll.
- New entries close after six hours (04:00 UTC for a 22:00 start). Any open
  trade still settles. The bot then sends the final report to the authorized
  Telegram user. `/report` retrieves it and `/wallet` shows virtual transfers
  grouped by destination.
- `/endsession` ends entries early. If an open position still has no result after
  30 minutes, the bot sends a provisional report with the remaining shares at
  cost, then follows up once the trade resolves. The report compares the
  share-weighted entry win rate with the average entry cost including estimated
  entry fees, using trades with verified outcomes. It reports early-sale
  P&L separately.
- The cutoff, open position, and virtual transfers survive a process restart
  with the same SQLite database. `/stop` pauses entries without extending the
  cutoff. Starting another session creates a fresh $10 paper bankroll and
  retains the previous trade and transfer history.

## Check an order-book quote

Run `python execution_check.py xrp down 10 0.82` to check the current XRP
five-minute DOWN market for a $10 budget and a maximum price of $0.82.
Use `btc`, `eth`, `sol`, or `xrp` and choose `up` or `down`.

The check reads the public book and the market's taker fee schedule. It reports
the best ask, visible shares at that exact price, estimated shares and fees,
and the source book's timestamp and hash. It rejects orders above the cap,
below the market minimum, or too large for the best ask. It uses a conservative
two-decimal share estimate. This is a snapshot, and liquidity can change before
an order reaches the exchange. The check has no credentials and places no order.

## Create Telegram credentials
1. In Telegram, open the verified @BotFather and send /newbot.
2. Save the token privately. Never commit it to GitHub.
3. Open your new bot and press Start.
4. Obtain your numeric Telegram user ID and set TELEGRAM_ALLOWED_USER_ID.

## Deploy on Oracle Cloud
1. Create an Ubuntu VM and confirm the selected shape is eligible for Always Free in the console. Availability and idle-instance policies apply.
2. In Advanced options, locate the cloud-init / initialization script field.
3. Paste deploy/oracle-cloud-init.sh after replacing REPO_URL, TELEGRAM_BOT_TOKEN and TELEGRAM_ALLOWED_USER_ID in the script. Do not commit your edited secret-bearing script.
4. Launch the VM. Cloud-init installs Python, clones the public repo, creates a venv, writes a root-only environment file and enables a systemd service.
5. Message the Telegram bot with /status then /start.

## Local tests
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python -m py_compile collector.py strategy.py backtest.py service.py

## Limitations
Market discovery depends on Polymarket's current five-minute event slug format. Paper fills use visible depth at one price level and estimated fees. The delay is a simulation setting. Actual matching, queue position, and deeper price levels are outside this model. A fresh $10 session starts only when requested.

## One-order live test (Windows Command Prompt)

This test runs from Command Prompt alongside the Telegram paper bot. It uses the
default five-minute entry rule and prints a paper estimate for the selected
order. It checks real fills and attempts redemption only if that order wins.

Install the optional live dependency and run the read-only preview:

```bat
.venv\Scripts\python.exe -m pip install -r requirements-live.txt
.venv\Scripts\python.exe live_test.py --cap 10
```

Run this on the network you intend to trade from. The preview checks Polymarket's
geographic restriction endpoint and waits up to five minutes for a qualifying
quote. It needs no wallet credentials. The cap is an upper bound for test wallet
funding. Actual order size is based on the book minimum and rounded up to the
next supported order amount. The preview displays the exact proposed amounts.

For the funded test, use an existing, dedicated Polymarket trading wallet with
no other orders or positions. Fund only the test amount, up to the specified
cap. Have its signer key and the Relayer API key and associated address from
**Polymarket → Settings → API Keys → Relayer API Keys** ready.
[Polymarket wallet setup](https://docs.polymarket.com/trading/wallets-auth)

```bat
.venv\Scripts\python.exe live_test.py --cap 10 --execute
```

Enter the keys only in your local terminal. Key input is hidden while typing.
Review the market, side, price and estimated maximum spend, then type `BUY` to
approve that candidate. The runner rechecks the current book, can lower the
limit and size, and submits at most one buy. It records confirmed fills and
compares the dedicated wallet's collateral change with the matched notional
to observe the fee. Avoid other activity in this wallet during the test.

The runner logs events to `data/live-test.jsonl`, including order and redemption
identifiers and measured timings. It leaves that journal in place and refuses
to submit another order if the outcome of an earlier attempt needs
reconciliation. On interruption or an unresolved status, check the journal and
your account before doing anything further. The test has no automatic
compounding or withdrawal.
