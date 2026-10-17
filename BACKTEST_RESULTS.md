# Backtest results (real Binance data, 10 coins, 2017-2026)

Data: Binance spot candles mirrored in the public GitHub repo `finom/static-klines` (spot-checked against known
prices). Costs: 0.10% fee + 0.05% slippage per side. Universe: BTC ETH BNB SOL XRP ADA DOGE AVAX LINK DOT.
Portfolio tests: 2018-06 -> 2026-10. "IS" = 2018-22, "OOS" = 2023 onward.

## 1. The original single-coin fast bot LOSES
Unprofitable on 0 of 10 coins (avg -16.5% out-of-sample vs +55% holding). A slower daily version only broke
even. Kept only as `/setmode single`.

## 2. Portfolio mode (default): daily trend-following, blended signal
Rules: for each of 10 coins, three trend signals (EMA50, 55-day Donchian breakout, 3-EMA ensemble with
inverse-volatility weights) are averaged; a coin is held only while in uptrend; total exposure is scaled so
trailing volatility matches the chosen risk profile; the rest sits in USDT; rebalanced DAILY with a 30% no-trade band.

| Strategy | Full CAGR | Sharpe | Max drawdown | CAGR excl. 2021 | 2023+: CAGR / Sharpe / max DD | 2024-26 total | Worst year |
|---|---|---|---|---|---|---|---|
| Hold same 10 coins | +48% | 0.90 | -82% | +6% | +30% / 0.73 / -72% | +3% | -74% |
| Hold BTC only | +33% | 0.78 | -77% | +30% | +53% / 1.14 / -53% | +94% | -64% |
| Conservative (vol 20%) | +28% | 1.22 | -35% | +18% | +21% / 0.99 / -29% | +31% | -27% |
| **Balanced (vol 30%), default** | **+39%** | **1.28** | **-44%** | **+24%** | **+26% / 0.97 / -34%** | **+50%** | **-32%** |
| Growth (vol 45%) | +50% | 1.33 | -48% | +29% | +29% / 0.95 / -41% | +53% | -32% |

Balanced by calendar year vs holding the basket: 2018 -16% vs -63% | 2019 +57% vs +61% | 2020 +130% vs +278% |
2021 +213% vs +1595% | 2022 -32% vs -74% | 2023 +60% vs +164% | 2024 +68% vs +94% | 2025 -12% vs -36% | 2026 YTD +2% vs -17%.

**The risk profiles are a dial, not a free lunch:** Sharpe is roughly constant, so more return comes with
proportionally bigger drawdowns. Growth earns more in good years and loses more in bad ones.

## 3. Ideas tested AGAINST A PRE-SET BAR (must beat baseline on BOTH IS and OOS, and excluding 2021)
| Idea | Result |
|---|---|
| Ensemble of EMA 20/50/100 | Better IS, worse OOS -> rejected alone |
| Inverse-volatility weighting | Better IS, slightly worse OOS -> rejected alone |
| Momentum-tilted weights | Worse IS, better OOS -> noise, rejected |
| Donchian 55/20 breakout alone | Better IS, equal OOS -> rejected alone |
| **Average of EMA + Donchian + ensemble/inv-vol (adopted)** | Better IS, equal OOS (0.97 vs 0.96), better ex-2021, worst year -32% vs -37% -> **adopted, marginal gain from diversifying signals** |
| Drawdown circuit breaker (halve exposure after -20%/-30%) | Cut IS drawdown but hurt OOS (Sharpe 0.84/0.72 vs 0.97) -> **rejected** |
| Long/short (profit in downtrends, needs futures) | Worst year ~-2%, but lower Sharpe (1.03 vs 1.19) and OOS (0.81 vs 0.96); ignores funding and liquidation risk -> **not built** |

Lesson: indicator tweaks sit on a plateau; differences of a few hundredths of Sharpe are noise.
The real levers are the **risk dial** and **idle cash** (the bot averages ~67% in USDT; at 4%/yr that would add
~2.7%/yr, but it is NOT implemented because Binance Earn integration could not be verified or tested here).

## 4. Robustness checks
* Trend length 20-100 days all gave Sharpe 1.3-1.4 (plateau). 4x higher costs barely changed results; a one-day
  execution delay still gave Sharpe ~1.3. Dropping SOL/BNB/DOGE (biggest hindsight winners) still beat holding.
* **WEEKLY rebalancing was a trap:** results depended on which weekday trades happen (2024-26 total from +5% to
  +97% for the same rules). Daily rebalancing is weekday-independent, so it is the default.
* **Engine == backtest:** the live PortfolioEngine (paper mode) replayed day by day over real 2024-26 data:
  +52.3% vs backtester +50.0%; max drawdown -33.0% vs -34.3%.

## 5. How to read this honestly
* Do not promise "+39%/year". Since 2023 it is about +26%/yr; over 2024-26, about +50% total (~14%/yr).
  Holding BTC alone beat it in raw return since 2023 (a hindsight winner). The edge is similar return with
  much smaller crashes.
* 2021 (alt-coin mania) inflates long-run averages: excluding it, ~+24%/yr.
* 30-45% drawdowns and losing years (2018 -16%, 2022 -32%, 2025 -12%) WILL happen again.
* Survivorship bias: the 10 coins are today's large coins; coins that died are missing, flattering absolute returns.
* The 25% exchange-side crash stop is a safety net and is not included in backtests.
* Past performance does not predict future results. Paper trade first; never risk money you cannot lose.

## 6. Idle-cash interest (optional `/setearn`, OFF by default) - scenario estimate
The bot averages 64-76% of its capital in USDT. If that cash earned interest in Binance Simple Earn (Flexible),
simple arithmetic on the backtest gives (CAGR full period; actual rates vary, are NOT guaranteed, and many accounts
get lower rates on larger balances):

| Profile | Avg idle cash | 0% | 2% | 4% | 6% |
|---|---|---|---|---|---|
| Conservative | 76% | +28.1% | +30.0% | +32.0% | +34.1% |
| Balanced | 69% | +38.8% | +40.7% | +42.7% | +44.7% |
| Growth | 64% | +50.0% | +51.9% | +53.8% | +55.8% |

Since 2023, balanced: +26.0%/yr -> +29.3%/yr at 4%. This is the lowest-risk profit lever available, but the Earn
code has been tested against a simulated exchange only. Counterparty risk is unchanged (funds stay on Binance).
Test with ~$20 before trusting it.

## 7. Independent cross-check with Freqtrade (battle-tested engine used as a referee)
Same data, same 2020-09-23 -> 2026-10-12 window, 10 coins, 0.1% fees, hold each coin while close > EMA50:

| Engine | Total return | Sharpe | Max drawdown |
|---|---|---|---|
| This project's backtester (daily-rebalanced 10% slots) | +3,152% | 1.41 | -52% |
| This project's fixed-stake replica (let winners run) | +7,536% | 1.47 | -57% |
| **Freqtrade 2026.9 (its own backtester)** | +4,801% | **1.48** | -46% (wallet) |

* **Risk-adjusted results agree across three independent implementations (Sharpe 1.41-1.48).** The edge in
  this sample is not an artifact of this project's code.
* **Total returns differ a lot** because they depend on stake rules and are dominated by one trade (a single DOGE
  position returned +10,000% in the 2021 mania). Headline totals are luck-sensitive; Sharpe is the stable number.
* Freqtrade's `lookahead-analysis` (20 signals): **no bias detected**. `recursive-analysis`: no bias once EMA warm-up
  is >=199 candles (the live bot fetches 500).
* **"Let winners run" (never trim) was tested with real position drift and rejected:** full-history CAGR +81% vs +39%,
  but since 2023 Sharpe fell 1.02 -> 0.84, drawdown deepened -33% -> -47%, worst year -31% -> -42%, and without 2021
  Sharpe fell 0.96 -> 0.79. The extra headline profit was concentration in 2021's mania winners.
* Not adopted from Freqtrade: hyperopt (overfits easily, and plateaus here already), FreqAI machine-learning
  (no evidence of edge), and the engine itself (GPL-3.0 licence; single-user design).

## 8. What leverage does to this strategy (research only; NOT offered by the bot)
Same blended trend strategy, with exposure allowed above 100% (leverage). Borrowing cost modelled at 8%/yr on the
levered part. Daily returns floored at -100%; real exchanges liquidate intraday, so true outcomes are WORSE than shown.

| Setting | CAGR | 2023+ CAGR | Max drawdown | Worst day | Days below -15% | Worst year |
|---|---|---|---|---|---|---|
| Spot, balanced (default) | +39% | +26% | -44% | -11% | 0 | -32% |
| Spot, growth | +50% | +29% | -48% | -17% | 1 | -32% |
| Up to 2x (vol target 60%) | +76% | +45% | -72% | -23% | 5 | -56% |
| Up to 3x (vol target 90%) | +103% | +54% | -88% | -34% | 23 | -73% |
| Up to 5x (vol target 150%) | +107% | +34% | -99% | -57% | 78 | -91% |

Lessons: leverage raises drawdowns much faster than returns; at 5x the 2023+ return is LOWER than at 3x (volatility
drag) while the account is effectively wiped out in drawdown. A 3x position is liquidated by a ~33% adverse move,
and the 3x row's worst day was -34%. Options were not backtested (no data available) and are not recommended:
buyers pay time decay and most contracts expire worthless; sellers collect small premiums with open-ended tail risk.
