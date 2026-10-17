"""Pure risk / accounting helpers (no I/O) so they are easy to test."""
import math
from typing import Any, Dict, Optional


def position_size(capital: float, risk_pct: float, sl_pct: float, max_position_pct: float) -> float:
    """
    USDT to deploy so that hitting the stop-loss loses ~risk_pct of capital,
    capped at max_position_pct of capital.

        size = capital * risk% / stop%      (capped)
    """
    if capital <= 0 or sl_pct <= 0 or risk_pct <= 0:
        return 0.0
    by_risk = capital * (risk_pct / 100.0) / (sl_pct / 100.0)
    cap = capital * (max_position_pct / 100.0)
    return max(0.0, min(by_risk, cap, capital * 0.95))


def floor_to_step(qty: float, step: float) -> float:
    if step <= 0:
        return qty
    return math.floor(qty / step + 1e-12) * step


def parse_fill(order: Dict[str, Any], base_asset: str, quote_asset: str, fee_rate: float) -> Dict[str, float]:
    """
    Normalise a Binance market-order response (or our paper order) into:
      qty        base asset actually held/removed after fees
      avg_price  volume-weighted fill price
      quote      gross quote amount traded
      fee_quote  fees paid in quote currency / BNB (NOT fees already netted out of qty)
    """
    fills = order.get("fills") or []
    if fills:
        qty = quote = fee_quote = fee_base = 0.0
        for f in fills:
            q, px = float(f["qty"]), float(f["price"])
            qty += q
            quote += q * px
            fee = float(f.get("commission", 0.0))
            asset = f.get("commissionAsset", "")
            if asset == base_asset:
                fee_base += fee  # already reflected by holding less base asset
            elif asset == quote_asset:
                fee_quote += fee
            else:  # e.g. BNB discount: approximate with the nominal rate
                fee_quote += q * px * fee_rate
        avg = quote / qty if qty else 0.0
        return {"qty": qty - fee_base, "avg_price": avg, "quote": quote, "fee_quote": fee_quote}

    qty = float(order.get("executedQty", 0.0))
    quote = float(order.get("cummulativeQuoteQty", 0.0))
    avg = quote / qty if qty else float(order.get("price", 0.0))
    fee_quote = float(order.get("fee_quote", quote * fee_rate))
    if order.get("side") == "BUY" and "fee_quote" not in order:
        qty_net = qty * (1.0 - fee_rate)  # fee taken from base asset on spot buys
    else:
        qty_net = qty
    return {"qty": qty_net, "avg_price": avg, "quote": quote, "fee_quote": fee_quote}


def format_to_step(value: float, step: float) -> str:
    """Floor `value` to the exchange step and render with exactly the decimals the step implies."""
    if step <= 0:
        return f"{value:.8f}".rstrip("0").rstrip(".")
    v = floor_to_step(value, step)
    decimals = max(0, -int(f"{step:e}".split("e")[1]))
    return f"{v:.{decimals}f}"


def ratchet_stop(entry: float, peak: float, current_stop: float, sl_pct: float, fee_rate: float) -> float:
    """
    Stops only ever move UP (never loosen):
      * after price has gained 1x the stop distance, lock in break-even (entry + round-trip fees);
      * after that, trail `sl_pct` below the highest price seen.
    """
    if entry <= 0 or peak <= 0:
        return current_stop
    new_stop = current_stop
    if peak >= entry * (1 + sl_pct / 100.0):
        breakeven = entry * (1 + 2 * fee_rate)
        trail = peak * (1 - sl_pct / 100.0)
        new_stop = max(current_stop, breakeven, trail)
    return max(current_stop, new_stop)
