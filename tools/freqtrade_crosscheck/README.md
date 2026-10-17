# Independent cross-check with Freqtrade

Freqtrade (GPL-3.0, battle-tested) is used here as a *referee*, not as the bot's engine: no Freqtrade code is
copied into this project. `TrendBasket.py` is a tiny strategy that holds each coin while its daily close is above
EMA(50), equal slots across 10 coins, i.e. the simplest form of the bot's idea.

```bash
pip install freqtrade
freqtrade download-data --exchange binance --pairs BTC/USDT ETH/USDT BNB/USDT SOL/USDT XRP/USDT ADA/USDT DOGE/USDT AVAX/USDT LINK/USDT DOT/USDT -t 1d --timerange 20190101-
freqtrade backtesting --config config.json --strategy-path . --strategy TrendBasket --timerange 20200923- --fee 0.001
freqtrade lookahead-analysis --config config.json --strategy-path . --strategy TrendBasket --timerange 20230101-20240101
freqtrade recursive-analysis --config config.json --strategy-path . --strategy TrendBasket --timerange 20230101-20240101
```
Results obtained (see BACKTEST_RESULTS.md section 7): Sharpe ~1.48, no look-ahead bias, no recursive-formula bias.
`research/unit_sim.py` simulates the bot's rebalancing with real position drift (used to test "let winners run").
