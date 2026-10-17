"""
Multi-indicator long-only signal engine.

The live bot and the backtester both call `compute_indicators` + `signal_at`,
so what you backtest is exactly what trades. All indicators are causal
(each value only depends on past/current closed candles), so computing them
once over a whole history introduces no look-ahead.

IMPORTANT: pass only CLOSED candles. The exchange wrapper drops the forming
candle; analysing it makes signals flicker and repaint.
"""
from dataclasses import dataclass, field, asdict
from typing import Literal, Dict, Any, List, Optional

import pandas as pd

SignalType = Literal["BUY", "SELL", "HOLD"]


@dataclass
class StrategyParams:
    ema_fast: int = 9
    ema_slow: int = 21
    rsi_period: int = 14
    rsi_oversold: float = 35.0
    rsi_overbought: float = 65.0
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal_period: int = 9
    bb_period: int = 20
    bb_std: float = 2.0
    min_buy_signals: int = 2
    min_sell_signals: int = 2
    trend_ema: int = 200  # only allow BUY when close > EMA(trend_ema); 0 disables

    def min_rows(self) -> int:
        return max(self.ema_slow, self.bb_period, self.macd_slow + self.macd_signal_period,
                   self.trend_ema) + 10

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class SignalResult:
    signal: SignalType = "HOLD"
    score: int = 0
    reasons: List[str] = field(default_factory=list)
    indicators: Dict[str, Any] = field(default_factory=dict)


def _close(df: pd.DataFrame) -> pd.Series:
    col = "close" if "close" in df.columns else ("Close" if "Close" in df.columns else df.columns[3])
    return df[col].astype(float)


def compute_indicators(df: pd.DataFrame, p: StrategyParams) -> pd.DataFrame:
    c = _close(df)
    out = pd.DataFrame(index=df.index)
    out["close"] = c
    out["ema_fast"] = c.ewm(span=p.ema_fast, adjust=False).mean()
    out["ema_slow"] = c.ewm(span=p.ema_slow, adjust=False).mean()

    delta = c.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1.0 / p.rsi_period, min_periods=p.rsi_period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / p.rsi_period, min_periods=p.rsi_period, adjust=False).mean()
    out["rsi"] = 100.0 - (100.0 / (1.0 + avg_gain / (avg_loss + 1e-10)))

    macd = c.ewm(span=p.macd_fast, adjust=False).mean() - c.ewm(span=p.macd_slow, adjust=False).mean()
    out["macd"] = macd
    out["macd_signal"] = macd.ewm(span=p.macd_signal_period, adjust=False).mean()

    mid = c.rolling(window=p.bb_period).mean()
    sd = c.rolling(window=p.bb_period).std(ddof=0)
    out["bb_upper"] = mid + p.bb_std * sd
    out["bb_lower"] = mid - p.bb_std * sd

    out["trend"] = c.ewm(span=p.trend_ema, adjust=False).mean() if p.trend_ema > 0 else float("nan")
    return out


def signal_at(ind: pd.DataFrame, i: int, p: StrategyParams, offset: int = 0) -> SignalResult:
    """Evaluate the signal using the indicator row at integer position i (needs i >= 1)."""
    # `offset` = number of earlier rows already used for warm-up but sliced off `ind`
    if i < 1 or i >= len(ind) or i + offset + 1 < p.min_rows():
        return SignalResult(reasons=["Insufficient data"])

    cur, prev = ind.iloc[i], ind.iloc[i - 1]
    price = float(cur["close"])
    buy = sell = 0
    reasons: List[str] = []

    if cur["ema_fast"] > cur["ema_slow"]:
        buy += 1
        reasons.append(f"EMA Bullish (EMA{p.ema_fast} > EMA{p.ema_slow})")
        if prev["ema_fast"] <= prev["ema_slow"]:
            buy += 1
            reasons.append("EMA Bullish Crossover")
    elif cur["ema_fast"] < cur["ema_slow"]:
        sell += 1
        reasons.append(f"EMA Bearish (EMA{p.ema_fast} < EMA{p.ema_slow})")
        if prev["ema_fast"] >= prev["ema_slow"]:
            sell += 1
            reasons.append("EMA Bearish Crossover")

    rsi = float(cur["rsi"])
    if rsi < p.rsi_oversold:
        buy += 1
        reasons.append(f"RSI Oversold ({rsi:.1f} < {p.rsi_oversold})")
    elif rsi > p.rsi_overbought:
        sell += 1
        reasons.append(f"RSI Overbought ({rsi:.1f} > {p.rsi_overbought})")

    if cur["macd"] > cur["macd_signal"]:
        buy += 1
        reasons.append("MACD Bullish (MACD > Signal)")
        if prev["macd"] <= prev["macd_signal"]:
            buy += 1
            reasons.append("MACD Bullish Crossover")
    elif cur["macd"] < cur["macd_signal"]:
        sell += 1
        reasons.append("MACD Bearish (MACD < Signal)")
        if prev["macd"] >= prev["macd_signal"]:
            sell += 1
            reasons.append("MACD Bearish Crossover")

    if price <= cur["bb_lower"]:
        buy += 1
        reasons.append(f"Price at Lower Bollinger Band ({price:.2f} <= {cur['bb_lower']:.2f})")
    elif price >= cur["bb_upper"]:
        sell += 1
        reasons.append(f"Price at Upper Bollinger Band ({price:.2f} >= {cur['bb_upper']:.2f})")

    signal: SignalType = "HOLD"
    score = max(buy, sell)
    if buy >= p.min_buy_signals and buy > sell:
        signal, score = "BUY", buy
    elif sell >= p.min_sell_signals and sell > buy:
        signal, score = "SELL", sell

    # Regime filter: no new longs below the long-term trend line.
    if signal == "BUY" and p.trend_ema > 0:
        trend = cur["trend"]
        if pd.isna(trend) or price <= float(trend):
            reasons.append(f"BUY blocked: price below EMA{p.trend_ema} trend filter")
            signal = "HOLD"

    indicators = {
        "price": round(price, 4),
        "ema_fast": round(float(cur["ema_fast"]), 4),
        "ema_slow": round(float(cur["ema_slow"]), 4),
        "rsi": round(rsi, 2),
        "macd": round(float(cur["macd"]), 4),
        "macd_signal": round(float(cur["macd_signal"]), 4),
        "bb_upper": round(float(cur["bb_upper"]), 4),
        "bb_lower": round(float(cur["bb_lower"]), 4),
        "buy_score": buy,
        "sell_score": sell,
    }
    return SignalResult(signal=signal, score=score, reasons=reasons, indicators=indicators)


def analyze(df: Optional[pd.DataFrame], params: Optional[StrategyParams] = None) -> SignalResult:
    """Analyse the latest closed candle of `df`."""
    p = params or StrategyParams()
    if df is None or len(df) < p.min_rows():
        return SignalResult(reasons=["Insufficient data"])
    return signal_at(compute_indicators(df, p), len(df) - 1, p)
