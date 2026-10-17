"""
Multi-coin daily portfolio backtester (long-only, no leverage).

Signals use day t's CLOSE; the resulting weights earn day t+1's return (no look-ahead).
Costs are charged on turnover (fee + slippage per unit traded). Cash earns 0%.

Strategies
  trend : hold every coin trading above its EMA(trend_len), equal weight (1/available coins)
  xs    : among coins above trend, hold the top_k by momentum over mom_len days, 1/top_k each
Optional overlay
  vol_target : scale total exposure so the strategy's trailing 60d volatility ~ target (never >100%)
"""
import glob
import os
from dataclasses import dataclass, replace
from typing import Optional

import numpy as np
import pandas as pd


@dataclass
class PCfg:
    mode: str = "trend"          # "trend" | "xs"
    trend_len: int = 100
    mom_len: int = 60
    top_k: int = 3
    vol_target: Optional[float] = None   # annualised, e.g. 0.35
    rebalance_days: int = 7
    phase: int = 0                       # which weekday-offset the weekly rebalance lands on
    cost_rate: float = 0.0015            # 0.10% fee + 0.05% slippage per unit of turnover
    signal: str = "ema"                  # "ema" | "ensemble" (avg of ema_set) | "donchian" (breakout)
    ema_set: tuple = (20, 50, 100)
    weighting: str = "equal"             # "equal" | "invvol" (risk parity-ish) | "mom" (momentum tilt)
    vol_win: int = 30
    mom_len: int = 60
    entry_n: int = 55                    # donchian: buy on new N-day high
    exit_n: int = 20                     # donchian: sell on new M-day low
    btc_gate: bool = False               # only hold alts if BTC itself is above its trend
    max_exposure: float = 1.0            # 1.0 = spot only. >1 simulates leverage (research only)
    band: float = 0.30                   # keep a holding's weight unless it moved >30% (relative)


def load_closes(data_dir: str, symbols, interval: str = "1d") -> pd.DataFrame:
    cols = {}
    for s in symbols:
        p = os.path.join(data_dir, f"{s}_{interval}.csv")
        if os.path.exists(p):
            cols[s] = pd.read_csv(p, parse_dates=[0], index_col=0)["close"].astype(float)
    return pd.DataFrame(cols).sort_index()


def _signal(close: pd.DataFrame, cfg: PCfg) -> pd.DataFrame:
    """Per-coin exposure signal in [0, 1] using only data up to each day's close."""
    avail = close.notna()
    if cfg.signal == "ema":
        ema = close.ewm(span=cfg.trend_len, adjust=False, min_periods=cfg.trend_len).mean()
        sig = ((close > ema) & avail).astype(float)
    elif cfg.signal == "ensemble":
        parts = []
        for L in cfg.ema_set:
            ema = close.ewm(span=L, adjust=False, min_periods=L).mean()
            parts.append(((close > ema) & avail).astype(float))
        sig = sum(parts) / len(parts)
    elif cfg.signal == "ema_ls":   # RESEARCH ONLY: long above trend, SHORT below (needs futures)
        ema = close.ewm(span=cfg.trend_len, adjust=False, min_periods=cfg.trend_len).mean()
        sig = ((close > ema).astype(float) - (close < ema).astype(float)).where(avail & ema.notna(), 0.0)
    elif cfg.signal == "donchian":
        hi = close.rolling(cfg.entry_n).max().shift(1)
        lo = close.rolling(cfg.exit_n).min().shift(1)
        c, h, l = close.to_numpy(), hi.to_numpy(), lo.to_numpy()
        st = np.zeros_like(c)
        for i in range(1, len(c)):
            prev = st[i - 1]
            enter = c[i] > h[i]
            leave = c[i] < l[i]
            st[i] = np.where(enter, 1.0, np.where(leave, 0.0, prev))
        sig = pd.DataFrame(st, index=close.index, columns=close.columns).where(avail, 0.0)
    else:
        raise ValueError(cfg.signal)
    if cfg.btc_gate and "BTCUSDT" in close:
        sig = sig.mul((sig["BTCUSDT"] > 0).astype(float), axis=0)
    return sig


def raw_weights(close: pd.DataFrame, cfg: PCfg) -> pd.DataFrame:
    """Unscaled, ungated target weights for every day."""
    avail = close.notna()
    n_avail = avail.sum(axis=1).clip(lower=1)

    if cfg.mode == "xs":
        ema = close.ewm(span=cfg.trend_len, adjust=False, min_periods=cfg.trend_len).mean()
        up = (close > ema) & avail
        mom = close / close.shift(cfg.mom_len) - 1.0
        score = mom.where(up & (mom > 0))
        rank = score.rank(axis=1, ascending=False, method="first")
        return ((rank <= cfg.top_k) & score.notna()).astype(float) / cfg.top_k

    if cfg.signal == "blend":
        # Model averaging: EMA trend + Donchian breakout + 3-EMA ensemble with inverse-vol weights.
        parts = [replace(cfg, signal="ema", weighting="equal"),
                 replace(cfg, signal="donchian", weighting="equal"),
                 replace(cfg, signal="ensemble", weighting="invvol")]
        return sum(raw_weights(close, c) for c in parts) / len(parts)

    sig = _signal(close, cfg)
    if cfg.weighting == "equal":
        return sig.div(n_avail, axis=0)
    if cfg.weighting == "invvol":
        vol = close.pct_change().rolling(cfg.vol_win, min_periods=cfg.vol_win).std()
        iv = (1.0 / vol).where(avail & (vol > 0))
        norm = iv.sum(axis=1).replace(0, np.nan)
        return sig.mul(iv.div(norm, axis=0)).fillna(0.0)
    if cfg.weighting == "mom":
        mom = close / close.shift(cfg.mom_len) - 1.0
        tilt = (0.5 + mom.rank(axis=1, pct=True)).where(avail)
        norm = tilt.sum(axis=1).replace(0, np.nan)
        return sig.mul(tilt.div(norm, axis=0)).fillna(0.0)
    raise ValueError(cfg.weighting)


def _gate(w: pd.DataFrame, every: int, band: float, phase: int = 0) -> pd.DataFrame:
    """Rebalance only every `every` days; on rebalance days keep an existing holding's weight
    unless it moved by more than `band` (relative) - avoids paying fees for tiny adjustments."""
    out = np.zeros_like(w.to_numpy(dtype=float))
    arr = w.to_numpy(dtype=float)
    prev = np.zeros(arr.shape[1])
    for i in range(len(arr)):
        if (i + phase) % max(every, 1) == 0:
            new = arr[i].copy()
            if band > 0:
                keep = (prev > 0) & (new > 0) & (np.abs(new / np.where(prev > 0, prev, 1) - 1) < band)
                new[keep] = prev[keep]
            prev = new
        out[i] = prev
    return pd.DataFrame(out, index=w.index, columns=w.columns)


def _vol_scale(raw: pd.DataFrame, rets: pd.DataFrame, cfg: PCfg) -> pd.Series:
    """Exposure multiplier (<=1) so the strategy's trailing 60d vol is about cfg.vol_target."""
    base = _gate(raw, cfg.rebalance_days, 0.0, cfg.phase)
    lag = base.shift(1).fillna(0.0)
    net = (lag * rets).sum(axis=1) - lag.diff().abs().sum(axis=1).fillna(0.0) * cfg.cost_rate
    vol = net.rolling(60, min_periods=30).std() * np.sqrt(365)
    return (cfg.vol_target / vol).replace([np.inf, -np.inf], 1.0).clip(upper=cfg.max_exposure).fillna(0.0)


def target_weights(close: pd.DataFrame, cfg: PCfg) -> pd.DataFrame:
    """Final weights per day as the bot would hold them (scaled, banded, weekly)."""
    rets = close.pct_change().fillna(0.0)
    w = raw_weights(close, cfg)
    if cfg.vol_target:
        w = w.mul(_vol_scale(w, rets, cfg), axis=0)
    return _gate(w, cfg.rebalance_days, cfg.band, cfg.phase)


def live_targets(close: pd.DataFrame, cfg: PCfg) -> dict:
    """Today's target weights (already vol-scaled) for the LAST closed day. Used by the live bot."""
    rets = close.pct_change().fillna(0.0)
    w = raw_weights(close, cfg)
    scale = 1.0
    if cfg.vol_target:
        scale = float(_vol_scale(w, rets, cfg).iloc[-1])
    row = (w.iloc[-1] * scale)
    return {"weights": {k: float(v) for k, v in row.items() if v > 0}, "scale": scale,
            "eligible": [k for k, v in w.iloc[-1].items() if v > 0]}


def run_portfolio(close: pd.DataFrame, cfg: PCfg, start: Optional[str] = None) -> pd.Series:
    """Daily net strategy returns (weights decided at t-1 close earn day-t return)."""
    rets = close.pct_change().fillna(0.0)
    w_lag = target_weights(close, cfg).shift(1).fillna(0.0)
    gross = (w_lag * rets).sum(axis=1)
    turnover = w_lag.diff().abs().sum(axis=1).fillna(0.0)
    net = gross - turnover * cfg.cost_rate
    return net.loc[start:] if start else net


def metrics(net: pd.Series) -> dict:
    net = net.dropna()
    if len(net) < 30:
        return {}
    eq = (1 + net).cumprod()
    years = len(net) / 365.0
    cagr = eq.iloc[-1] ** (1 / years) - 1
    vol = net.std() * np.sqrt(365)
    dd = (eq / eq.cummax() - 1).min()
    return {"total_pct": (eq.iloc[-1] - 1) * 100, "cagr_pct": cagr * 100, "vol_pct": vol * 100,
            "sharpe": (net.mean() * 365) / vol if vol > 0 else 0.0, "maxdd_pct": dd * 100,
            "calmar": (cagr / abs(dd)) if dd < 0 else 0.0}


def hold_returns(close: pd.DataFrame, symbols=None, start=None) -> pd.Series:
    """Buy-and-hold benchmark: equal-weight daily-rebalanced basket of available coins."""
    c = close[symbols] if symbols else close
    r = c.pct_change()
    net = r.mean(axis=1).fillna(0.0)
    return net.loc[start:] if start else net
