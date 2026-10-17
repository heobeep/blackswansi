"""
Honest backtester for the bot's strategy.

    python -m app.backtest --symbol BTCUSDT --interval 1h --days 730
    python -m app.backtest --csv mydata.csv --optimize

Realism rules (the things that make most backtests lie, handled here):
  * Signals use bar i's CLOSE; orders fill at bar i+1's OPEN (no look-ahead).
  * Fees on both sides + slippage on every fill.
  * Stop-loss / take-profit are checked against each bar's HIGH/LOW. If both
    fall inside one bar the STOP is assumed to hit first (pessimistic).
    Gaps through a stop fill at the open, not at the stop price.
  * Same cooldown + position-sizing + trend-filter code path as the live bot.
  * Results are compared with buy-and-hold, and parameter optimisation is
    judged ONLY on a held-out out-of-sample slice.
"""
import argparse
import csv
import itertools
import json
import math
import os
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, replace
from typing import List, Optional, Tuple

import numpy as np
import pandas as pd

from app.trading.risk import position_size, ratchet_stop
from app.trading.strategy import StrategyParams, compute_indicators, signal_at

BARS_PER_YEAR = {"1m": 525600, "3m": 175200, "5m": 105120, "15m": 35040, "30m": 17520,
                 "1h": 8760, "2h": 4380, "4h": 2190, "1d": 365}
INTERVAL_MS = {"1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
               "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000, "1d": 86_400_000}


@dataclass
class BacktestConfig:
    capital: float = 1000.0
    risk_pct: float = 1.0
    sl_pct: float = 2.0
    tp_pct: float = 4.0
    max_position_pct: float = 30.0
    fee_rate: float = 0.001
    slippage_rate: float = 0.0005
    cooldown_bars: int = 3
    daily_loss_pct: float = 0.0     # 0 = off in backtests unless bars/day known
    interval: str = "1h"
    trailing: bool = True           # mirror the live bot's break-even + trailing stop


@dataclass
class Trade:
    entry_idx: int
    exit_idx: int
    entry_price: float
    exit_price: float
    pnl: float
    reason: str


@dataclass
class Result:
    trades: List[Trade] = field(default_factory=list)
    equity: Optional[pd.Series] = None
    bars: int = 0
    config: Optional[BacktestConfig] = None

    # ---- metrics -------------------------------------------------------
    def metrics(self) -> dict:
        eq = self.equity
        cap0 = self.config.capital
        final = float(eq.iloc[-1]) if eq is not None and len(eq) else cap0
        ret = final / cap0 - 1.0
        rets = eq.pct_change().dropna() if eq is not None else pd.Series(dtype=float)
        ppy = BARS_PER_YEAR.get(self.config.interval, 8760)
        sharpe = float(rets.mean() / rets.std() * math.sqrt(ppy)) if len(rets) > 2 and rets.std() > 0 else 0.0
        peak = eq.cummax() if eq is not None else pd.Series(dtype=float)
        mdd = float(((eq - peak) / peak).min()) if eq is not None and len(eq) else 0.0
        wins = [t.pnl for t in self.trades if t.pnl > 0]
        losses = [t.pnl for t in self.trades if t.pnl <= 0]
        gross_loss = -sum(losses)
        pf = (sum(wins) / gross_loss) if gross_loss > 0 else (float("inf") if wins else 0.0)
        years = max(self.bars / ppy, 1e-9)
        return {
            "return_pct": ret * 100,
            "cagr_pct": ((final / cap0) ** (1 / years) - 1) * 100 if final > 0 else -100.0,
            "max_drawdown_pct": mdd * 100,
            "sharpe": sharpe,
            "trades": len(self.trades),
            "win_rate_pct": (len(wins) / len(self.trades) * 100) if self.trades else 0.0,
            "profit_factor": pf,
            "avg_trade_pnl": float(np.mean([t.pnl for t in self.trades])) if self.trades else 0.0,
            "final_equity": final,
        }


def simulate(df: pd.DataFrame, params: StrategyParams, cfg: BacktestConfig,
             indicators: Optional[pd.DataFrame] = None, offset: int = 0) -> Result:
    """Run the strategy over `df` (columns open/high/low/close, closed candles only)."""
    ind = indicators if indicators is not None else compute_indicators(df, params)
    o, h, l, c = (df[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    n = len(df)
    cash = cfg.capital
    pos = None  # dict(qty, entry, cost, sl, tp, idx)
    last_exit = -10**9
    trades: List[Trade] = []
    equity = np.empty(n)
    pending: Optional[str] = None  # "BUY"/"SELL" decided at previous close, fills at this open
    fee, slip = cfg.fee_rate, cfg.slippage_rate

    def close_pos(i: int, raw_price: float, reason: str):
        nonlocal cash, pos, last_exit
        px = raw_price * (1 - slip)
        gross = pos["qty"] * px
        proceeds = gross * (1 - fee)
        pnl = proceeds - pos["cost"]
        cash += proceeds
        trades.append(Trade(pos["idx"], i, pos["entry"], px, pnl, reason))
        pos, last_exit = None, i

    for i in range(n):
        # 1) execute orders decided on the previous bar's close, at this bar's open
        if pending == "BUY" and pos is None:
            alloc = position_size(cash, cfg.risk_pct, cfg.sl_pct, cfg.max_position_pct)
            if alloc >= 10.0:
                px = o[i] * (1 + slip)
                fee_amt = alloc * fee
                qty = (alloc - fee_amt) / px
                cash -= alloc
                pos = {"qty": qty, "entry": px, "cost": alloc, "idx": i, "peak": px,
                       "sl": px * (1 - cfg.sl_pct / 100), "tp": px * (1 + cfg.tp_pct / 100)}
        elif pending == "SELL" and pos is not None:
            close_pos(i, o[i], "signal")
        pending = None

        # 2) intrabar stop / target on the open position (stop wins ties)
        if pos is not None:
            if o[i] <= pos["sl"]:
                close_pos(i, o[i], "stop (gap)")
            elif l[i] <= pos["sl"]:
                close_pos(i, pos["sl"], "stop")
            elif o[i] >= pos["tp"]:
                close_pos(i, o[i], "target (gap)")
            elif h[i] >= pos["tp"]:
                close_pos(i, pos["tp"], "target")

        # 2b) ratchet the stop using this bar's CLOSE (conservative: applies from the next bar)
        if pos is not None and cfg.trailing:
            pos["peak"] = max(pos["peak"], c[i])
            pos["sl"] = ratchet_stop(pos["entry"], pos["peak"], pos["sl"], cfg.sl_pct, cfg.fee_rate)

        # 3) mark to market
        equity[i] = cash + (pos["qty"] * c[i] if pos else 0.0)

        # 4) decide on this bar's close -> executes next bar
        if i < n - 1:
            sig = signal_at(ind, i, params, offset).signal
            if sig == "BUY" and pos is None and (i - last_exit) > cfg.cooldown_bars:
                pending = "BUY"
            elif sig == "SELL" and pos is not None:
                pending = "SELL"

    if pos is not None:  # liquidate at the end so results are realised
        close_pos(n - 1, c[-1], "end of data")
        equity[-1] = cash

    return Result(trades=trades, equity=pd.Series(equity, index=df.index), bars=n, config=cfg)


def buy_and_hold(df: pd.DataFrame, cfg: BacktestConfig) -> dict:
    px0 = df["open"].iloc[1] * (1 + cfg.slippage_rate)
    qty = cfg.capital * (1 - cfg.fee_rate) / px0
    eq = qty * df["close"]
    peak = eq.cummax()
    return {"return_pct": (eq.iloc[-1] / cfg.capital - 1) * 100,
            "max_drawdown_pct": float(((eq - peak) / peak).min() * 100)}


# ---- data ------------------------------------------------------------------
def fetch_klines(symbol: str, interval: str, days: int, cache_dir: str = "data/cache") -> pd.DataFrame:
    """Download historical klines from Binance's public API (no key needed), cached as CSV."""
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, f"{symbol}_{interval}_{days}d.csv")
    if os.path.exists(path) and time.time() - os.path.getmtime(path) < 6 * 3600:
        return load_csv(path)

    step = INTERVAL_MS[interval]
    end = int(time.time() * 1000)
    start = end - days * 86_400_000
    rows = []
    while start < end:
        q = urllib.parse.urlencode({"symbol": symbol, "interval": interval, "startTime": start, "limit": 1000})
        with urllib.request.urlopen(f"https://api.binance.com/api/v3/klines?{q}", timeout=20) as r:
            chunk = json.loads(r.read())
        if not chunk:
            break
        rows += chunk
        start = chunk[-1][0] + step
        time.sleep(0.2)
    df = pd.DataFrame(rows, columns=["open_time", "open", "high", "low", "close", "volume", "close_time",
                                     "qv", "nt", "tb", "tq", "ig"])
    df = df[df["close_time"] < int(time.time() * 1000)]  # closed candles only
    df = df[["open_time", "open", "high", "low", "close", "volume"]].astype(float)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
    df = df.drop_duplicates("open_time").set_index("open_time")
    df.to_csv(path)
    return df


def load_csv(path: str) -> pd.DataFrame:
    df = pd.read_csv(path, parse_dates=[0], index_col=0)
    df.columns = [c.lower() for c in df.columns]
    return df[["open", "high", "low", "close"] + (["volume"] if "volume" in df.columns else [])].astype(float)


# ---- evaluation ---------------------------------------------------------------
GRID = {
    "ema_fast": [9, 12],
    "ema_slow": [21, 34],
    "min_buy_signals": [2, 3],
    "trend_ema": [0, 200],
}


def optimise(train: pd.DataFrame, base: StrategyParams, cfg: BacktestConfig) -> Tuple[StrategyParams, float]:
    """Pick params by in-sample Sharpe (tiny grid on purpose: big grids just overfit)."""
    best, best_score = base, -1e9
    keys = list(GRID)
    for combo in itertools.product(*GRID.values()):
        p = replace(base, **dict(zip(keys, combo)))
        if p.ema_fast >= p.ema_slow:
            continue
        m = simulate(train, p, cfg).metrics()
        score = m["sharpe"] if m["trades"] >= 10 else -1e6
        if score > best_score:
            best, best_score = p, score
    return best, best_score


def verdict(is_m: dict, oos_m: dict, oos_bh: dict) -> str:
    reasons = []
    if oos_m["trades"] < 30:
        reasons.append(f"only {oos_m['trades']} out-of-sample trades (<30: not statistically meaningful)")
    if oos_m["return_pct"] <= 0:
        reasons.append("out-of-sample return is not positive after fees")
    if oos_m["profit_factor"] < 1.2:
        reasons.append(f"profit factor {oos_m['profit_factor']:.2f} < 1.2 (too thin to survive real-world costs)")
    if oos_m["return_pct"] < oos_bh["return_pct"] and oos_m["max_drawdown_pct"] <= oos_bh["max_drawdown_pct"] * 0.8:
        pass  # lower return but much lower drawdown is acceptable
    elif oos_m["return_pct"] < oos_bh["return_pct"]:
        reasons.append("did not beat simply holding on a risk-adjusted basis")
    if is_m["return_pct"] > 0 and oos_m["return_pct"] <= 0:
        reasons.append("worked in-sample but failed out-of-sample (classic overfit)")
    if not reasons:
        return "PASS: no red flags on this data. That is NOT proof of future profit. Paper-trade it before risking money."
    return "NO EDGE DEMONSTRATED: " + "; ".join(reasons)


def fmt(m: dict) -> str:
    pf = "inf" if m["profit_factor"] == float("inf") else f"{m['profit_factor']:.2f}"
    return (f"return {m['return_pct']:+.1f}% | maxDD {m['max_drawdown_pct']:.1f}% | Sharpe {m['sharpe']:.2f} | "
            f"trades {m['trades']} | win {m['win_rate_pct']:.0f}% | PF {pf}")


def run(df: pd.DataFrame, cfg: BacktestConfig, params: StrategyParams, do_optimise: bool, split: float = 0.6) -> dict:
    cut = int(len(df) * split)
    train, test = df.iloc[:cut], df.iloc[cut:]
    out = {}
    chosen = params
    if do_optimise:
        chosen, _ = optimise(train, params, cfg)
    # indicators computed on the full history so the test slice has warm-up; sliced afterwards (causal => no leak)
    full_ind = compute_indicators(df, chosen)
    is_res = simulate(train, chosen, cfg, indicators=full_ind.iloc[:cut])
    oos_res = simulate(test, chosen, cfg, indicators=full_ind.iloc[cut:], offset=cut)
    out.update(params=chosen.as_dict(), in_sample=is_res.metrics(), out_of_sample=oos_res.metrics(),
               bh_in_sample=buy_and_hold(train, cfg), bh_out_of_sample=buy_and_hold(test, cfg))
    out["verdict"] = verdict(out["in_sample"], out["out_of_sample"], out["bh_out_of_sample"])
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--interval", default="1h", choices=list(INTERVAL_MS))
    ap.add_argument("--days", type=int, default=730)
    ap.add_argument("--csv", help="Use a local OHLCV CSV instead of downloading")
    ap.add_argument("--optimize", action="store_true", help="Tune params in-sample, report out-of-sample")
    ap.add_argument("--capital", type=float, default=1000)
    ap.add_argument("--sl", type=float, default=2.0)
    ap.add_argument("--tp", type=float, default=4.0)
    ap.add_argument("--risk", type=float, default=1.0)
    ap.add_argument("--no-trend-filter", action="store_true")
    ap.add_argument("--no-trailing", action="store_true")
    a = ap.parse_args()

    df = load_csv(a.csv) if a.csv else fetch_klines(a.symbol, a.interval, a.days)
    cfg = BacktestConfig(capital=a.capital, sl_pct=a.sl, tp_pct=a.tp, risk_pct=a.risk, interval=a.interval, trailing=not a.no_trailing)
    params = StrategyParams(trend_ema=0 if a.no_trend_filter else 200)
    res = run(df, cfg, params, a.optimize)

    print(f"\n{len(df)} candles  {df.index[0]} → {df.index[-1]}  ({a.symbol} {a.interval})")
    print(f"Params: {res['params']}\n")
    print(f"IN-SAMPLE (first 60%)     : {fmt(res['in_sample'])}")
    print(f"   buy & hold             : {res['bh_in_sample']['return_pct']:+.1f}%  (maxDD {res['bh_in_sample']['max_drawdown_pct']:.1f}%)")
    print(f"OUT-OF-SAMPLE (last 40%)  : {fmt(res['out_of_sample'])}")
    print(f"   buy & hold             : {res['bh_out_of_sample']['return_pct']:+.1f}%  (maxDD {res['bh_out_of_sample']['max_drawdown_pct']:.1f}%)")
    print(f"\nVERDICT: {res['verdict']}\n")


if __name__ == "__main__":
    main()
