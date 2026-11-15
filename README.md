# Polymarket 5-Minute Telegram Paper-Trading Bot

Paper-only research software. Telegram controls monitoring and reports. The engine reads public Polymarket Gamma/CLOB data, simulates positions and records outcomes. It has no wallet, private keys, signing or live-order endpoint.

## Strategy defaults
- Heuristic discovery of crypto five-minute UP/DOWN markets.
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
Market discovery is heuristic and may miss markets or include false positives. Inspect each market's exact question and resolution terms. The bot uses best ask as a paper fill and does not model depth walking, partial fills, latency, queue position, impact or market-specific fees. The settlement heuristic is not a substitute for verifying official resolution. No profitability claim is made. Historical snapshots and verified settlements are required for meaningful backtesting.
