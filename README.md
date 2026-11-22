# Polymarket 5-Minute Telegram Paper-Trading Bot

Paper-only research software. Telegram controls monitoring and reports. The engine reads public Polymarket Gamma/CLOB data, simulates positions and records outcomes. It has no wallet, private keys, signing or live-order endpoint.

## Strategy defaults
- Looks up current BTC, ETH, SOL, and XRP five-minute UP/DOWN markets.
- Entry during final 120 seconds.
- Trigger: best executable ask >= 0.80.
- Choose the highest qualifying ask among discovered eligible markets.
- Allocate 100% of virtual cash; one open position at a time.
- Settlement heuristic: Gamma market is closed and one of two outcome prices is >= 0.99.

An 80-cent contract is not proof of an 80% true win probability. A losing all-in position can wipe out the virtual bankroll. This is a research setting, not a recommendation.

## Telegram commands
/start resume monitoring; /stop pause new entries; /status; /balance; /trades; /stats; /markets; /settings; /help.
Change settings with /set threshold 0.82, /set entry_window 120, or /set poll_seconds 2.
Only TELEGRAM_ALLOWED_USER_ID can control the bot.

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
Market discovery depends on Polymarket's current five-minute event slug format. Inspect each market's exact question and resolution terms. The bot uses best ask as a paper fill and does not model depth walking, partial fills, latency, queue position, impact or market-specific fees. The settlement heuristic is not a substitute for verifying official resolution. No profitability claim is made. Historical snapshots and verified settlements are required for meaningful backtesting.
