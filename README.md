# BlackSwanSI

Long-only spot bot for Binance, run through Telegram with a Mini App dashboard.
Paper trading by default. **Nothing here is a promise of profit.**

## Read this first

* **No strategy is guaranteed to make money.** This one combines EMA/RSI/MACD/Bollinger
  signals with a trend filter. Whether it has an edge on the pair/timeframe you care about
  is an empirical question — **run the backtest before anything else**.
* Live trading is **off by default** (`ALLOW_LIVE_TRADING=false`). Users can store keys, but
  no real order is sent until the operator flips that switch.
* Never run it live with money you cannot afford to lose. Crypto can drop 20% in an hour.

## What it does and what it honestly delivers (read `BACKTEST_RESULTS.md`)
**Portfolio mode (default):** daily trend-following across 10 large coins. Three trend signals are averaged; a coin
is held only while trending up; position sizes target a chosen volatility; everything else sits in USDT.
Verified on real 2018-2026 data (balanced profile): about **+26%/yr since 2023**, worst drop **-34%** (holding the
same coins: -72%); over 2024-26 about **+50% total**. Holding BTC alone did better in raw return since 2023.
It is a **risk-reduced growth strategy, not a get-rich machine**, and losing years (2022: -32%) happen.
`/setrisk conservative|balanced|growth` is the honest profit dial: more risk = more return AND bigger drops.
The old single-coin strategy lost money on all 10 coins; it remains only as `/setmode single`.
Recommended capital: **$300+** (positions must be >= $10). In paper mode use `/setcapital 1000`.

## Why not build on Freqtrade?
Freqtrade is excellent, but it would not raise profit: it executes strategies, it does not create an edge. It is also
GPL-3.0 (it would force this project under GPL) and is built for one operator per instance, whereas this bot is a
multi-user Telegram service. It is used here as an independent referee instead: see `tools/freqtrade_crosscheck/`
and BACKTEST_RESULTS.md section 7. Single-user? Freqtrade + `TrendBasket.py` is a legitimate alternative.

## Idle-cash interest (optional, OFF by default)
The bot sits in USDT most of the time. With `ENABLE_EARN=true` (operator) and `/setearn on` (user), the bot's own
idle cash - never other money in the account - is parked in Binance Simple Earn (Flexible) and redeemed on demand
before buys. Interest is tracked separately from trading PnL. Any Earn error is logged and skipped: it can never block
trading. Uses the same API permission the bot already has (withdrawals stay OFF). **Tested on a simulated exchange
only: try it with ~$20 first.** Rates change and are not guaranteed (see BACKTEST_RESULTS.md section 6).

## The dashboard (Telegram Mini App) - honest by design
Shows true equity (cash + live value of holdings), the real starting balance, each coin held with its weight and
profit/loss, a PAPER/LIVE label, and a "Know your risk" card with the historical worst drop and a reminder that
losing years happen. If the server cannot be reached it shows a warning banner and **never substitutes sample data**.
Outside Telegram it runs a clearly-labelled red DEMO with sample numbers only.

## Re-test the single-coin strategy yourself (no keys, no risk)

```bash
pip install -r requirements-dev.txt
python -m app.backtest --symbol BTCUSDT --interval 1h --days 730 --optimize
```

It downloads public Binance candles and reports in-sample vs **out-of-sample** results,
net of fees and slippage, against buy-and-hold, and prints a blunt verdict. Try several
symbols/timeframes. If it says `NO EDGE DEMONSTRATED`, believe it. Backtest realism rules
(next-bar fills, pessimistic stops, fees) are listed at the top of `app/backtest.py`.

A PASS is **not** proof — it is "no red flags yet". Follow with weeks of paper trading.

## Deploy
See **DEPLOY_BLACKSWANSI.md** for the step-by-step Render guide (free plan works for paper trading; upgrade before real money) or use `docker compose up -d --build` locally.

## Step 2 — run it

```bash
cp .env.example .env
python -m app.security genkey            # → ENCRYPTION_KEY
python -c "import secrets;print(secrets.token_urlsafe(32))"   # → WEBHOOK_SECRET
# fill BOT_TOKEN, ADMIN_IDS, WEBHOOK_URL in .env
docker compose up -d --build
```
Local dev: `DEV_MODE=true` (polling; dashboard auth bypass works from localhost only).

## What protects users and operator

| Risk | Mitigation |
|---|---|
| DB leak exposes exchange keys | Keys encrypted at rest (Fernet); legacy plaintext auto-migrated on start |
| Key can drain the account | `/setkeys` **rejects keys with withdrawals enabled**; warns if no IP whitelist |
| Forged Telegram webhook calls | `X-Telegram-Bot-Api-Secret-Token` verified in constant time |
| Replayed Mini App login data | `initData` older than 24h rejected |
| Accidental real trading | `ALLOW_LIVE_TRADING` master switch, default off |
| Over-sizing | Position sized so a stop-out loses ≈ `risk_percent` of capital, capped at `MAX_POSITION_PCT` |
| Death spirals | Daily loss limit, post-exit cooldown |
| Signal repainting | Only **closed** candles are analysed |
| Fake PnL | Real fill prices/quantities, fees included, paper mode simulates fees + slippage |
| Live sell failures | Base-asset fees netted from qty; quantity floored to Binance LOT_SIZE |
| Bot crashes / loses connection mid-trade | Protective stop-loss is an **exchange-side order** on Binance; live mode never holds a position without one (flattens if it can't be placed) |
| Book drifts from reality | Every live cycle reconciles with Binance: books exchange-stop fills, re-arms cancelled stops, detects manual sales |
| Giving back open profit | Stop ratchets to break-even after +1R, then trails; it never loosens |
| Container escape blast radius | Non-root user, dropped capabilities |

## Known limitations (be honest with yourself and your users)

* The exchange-side stop is a STOP_LOSS_LIMIT (limit 0.5% below trigger). In a violent gap
  it can fail to fill; the bot's own check then sells at market. Reduce risk, not eliminate it.
* **The live-order code has been unit-tested against a simulated exchange only.** Before real
  money: run it live with the smallest possible amount (~$15-20) and watch the first trades.
* Long-only, one position per user, one pair at a time. Spot only.
* Single process + SQLite: fine for tens of users, not thousands.
* Running this for paying users that hold their own keys may have legal/regulatory
  implications where you operate. Get advice before charging for it.

## Tests

```bash
pytest
```

## Code provenance and licensing

All code here is original. The *designs* for exchange-side stops, reconciliation and trailing
stops follow patterns common to mature bots (Freqtrade, Hummingbot). Freqtrade is GPL-3.0, so
none of its source was copied; copying it would force this project under GPL. Hummingbot
(Apache-2.0), Jesse (MIT) and CCXT (MIT) are permissive and safe to reuse with attribution.
