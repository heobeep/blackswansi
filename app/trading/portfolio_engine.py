"""
Portfolio mode: the strategy that was actually validated on real data (see BACKTEST_RESULTS.md).

  * Universe: Config.PORTFOLIO_COINS (default 10 large USDT pairs)
  * A coin is held only while its daily close is above its EMA(PORTFOLIO_TREND_EMA)
  * Position size = 1/N of equity per coin, scaled down so trailing volatility ~ PORTFOLIO_VOL_TARGET
  * Rebalance every PORTFOLIO_REBALANCE_DAYS days (UTC), with a 30% no-trade band
  * Everything not invested stays in USDT (cash)
  * Each position carries a wide exchange-side "disaster" stop (DISASTER_STOP_PCT) in case of a crash
    between rebalances or if this server is offline. (Not part of the backtest: a safety net only.)

Decisions use CLOSED daily candles only; orders execute right after the daily close.
"""
import logging
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

import pandas as pd

from app.config import Config
from app.database import Database
from app.portfolio_backtest import PCfg, live_targets
from app.trading.exchange import Exchange
from app.trading.risk import parse_fill

logger = logging.getLogger(__name__)

BAND = 0.30
CLOSES_TTL = 1800  # seconds; market data is identical for every user, so fetch it once and share
_CLOSES_CACHE: Dict[str, Any] = {"t": 0.0, "key": None, "df": None}


class PortfolioEngine:
    def __init__(self, user_id: int, db: Database, notifier: Callable):
        self.user_id = user_id
        self.db = db
        self.notifier = notifier
        self.user: Dict[str, Any] = {}
        self.exchange: Optional[Exchange] = None
        self._cycle = 0
        self.cfg = PCfg(mode="trend", trend_len=Config.PORTFOLIO_TREND_EMA, signal=Config.PORTFOLIO_SIGNAL,
                        vol_target=Config.PORTFOLIO_VOL_TARGET or None,
                        rebalance_days=Config.PORTFOLIO_REBALANCE_DAYS, band=BAND)

    # ------------------------------------------------------------------ lifecycle
    async def initialize(self) -> None:
        self.user = await self.db.get_user(self.user_id) or {}
        self.exchange = Exchange(self.user.get("binance_api_key", ""), self.user.get("binance_secret_key", ""))
        logger.info(f"PortfolioEngine ready for user {self.user_id} (paper={self.exchange.is_paper})")

    async def close(self) -> None:
        if self.exchange:
            await self.exchange.close()

    async def _notify(self, msg: str) -> None:
        if self.notifier:
            try:
                await self.notifier(self.user_id, msg)
            except Exception as e:
                logger.error(f"Notifier error: {e}")

    # ------------------------------------------------------------------ main cycle
    async def run_once(self) -> str:
        try:
            user = await self.db.get_user(self.user_id)
            if not user:
                return f"User {self.user_id} not found"
            self.user = user
            if not user.get("is_trading"):
                return "stopped"
            # per-user risk profile -> volatility target
            self.cfg.vol_target = Config.RISK_PROFILES.get(user.get("risk_profile") or "balanced",
                                                           Config.PORTFOLIO_VOL_TARGET)
            if self.exchange is None:
                self.exchange = Exchange(user.get("binance_api_key", ""), user.get("binance_secret_key", ""))

            if not self.exchange.is_paper:
                await self._reconcile()
            earn_on = (not self.exchange.is_paper) and Config.ENABLE_EARN
            if earn_on:
                await self._earn_sync_income()
                if not user.get("earn_enabled") and float(user.get("earn_principal") or 0) > 0.01:
                    await self._earn_redeem(float(user["earn_principal"]))   # user opted out: bring it all back

            prices = await self._prices()
            await self._check_disaster_stops(prices)

            action = "HOLD"
            if await self._rebalance_due():
                done = await self._rebalance(prices)
                action = "REBALANCED" if done else "REBALANCE_SKIPPED"

            if earn_on and self.user.get("earn_enabled"):
                await self._earn_sweep(prices)

            self._cycle += 1
            if self._cycle % 6 == 1:
                eq = await self._equity(prices)
                await self.db.save_snapshot(self.user_id, equity=round(eq, 4),
                                            price=prices.get("BTCUSDT", 0.0))
            return f"User {self.user_id} portfolio | {action}"
        except Exception as e:
            logger.error(f"Portfolio cycle error for user {self.user_id}: {e}", exc_info=True)
            return f"Error: {e}"

    # ------------------------------------------------------------------ helpers
    async def _prices(self) -> Dict[str, float]:
        out: Dict[str, float] = {}
        symbols = set(Config.PORTFOLIO_COINS) | {p["symbol"] for p in await self.db.get_portfolio_positions(self.user_id)}
        for sym in symbols:
            px = await self.exchange.get_price(sym)
            if px and px > 0:
                out[sym] = px
        return out

    async def _equity(self, prices: Dict[str, float]) -> float:
        capital = float(self.user.get("capital", 0.0))
        unreal = 0.0
        for p in await self.db.get_portfolio_positions(self.user_id):
            px = prices.get(p["symbol"], p["entry_price"])
            unreal += p["quantity"] * px - p["entry_cost"]
        return capital + unreal

    async def _rebalance_due(self) -> bool:
        today = datetime.utcnow().date()
        last = (self.user.get("last_rebalance") or "")
        if last == today.isoformat():
            return False
        if not last:
            return True  # first run: invest immediately
        return today.toordinal() % max(Config.PORTFOLIO_REBALANCE_DAYS, 1) == 0

    async def _closes(self) -> Optional[pd.DataFrame]:
        key = tuple(Config.PORTFOLIO_COINS)
        c = _CLOSES_CACHE
        if c["df"] is not None and c["key"] == key and time.time() - c["t"] < CLOSES_TTL:
            return c["df"]
        cols = {}
        for sym in Config.PORTFOLIO_COINS:
            df = await self.exchange.get_klines(sym, "1d", limit=500)
            if df is not None and len(df) > self.cfg.trend_len + 70:
                cols[sym] = df["close"]
        if len(cols) < max(3, len(Config.PORTFOLIO_COINS) // 2):
            logger.error(f"User {self.user_id}: only {len(cols)} coins had usable data; skipping rebalance")
            return None
        df_all = pd.DataFrame(cols).sort_index()
        _CLOSES_CACHE.update(t=time.time(), key=key, df=df_all)
        return df_all

    # ------------------------------------------------------------------ rebalance
    async def _rebalance(self, prices: Dict[str, float]) -> bool:
        close = await self._closes()
        if close is None:
            return False
        tgt = live_targets(close, self.cfg)
        weights: Dict[str, float] = tgt["weights"]
        positions = {p["symbol"]: p for p in await self.db.get_portfolio_positions(self.user_id)}
        equity = await self._equity(prices)
        log: List[str] = []

        # 1) exits and resizes first (frees cash)
        for sym, pos in positions.items():
            price = prices.get(sym)
            if not price:
                continue
            w = weights.get(sym, 0.0)
            if w <= 0:
                if await self._sell(sym, price, 1.0, "Trend exit (below EMA)"):
                    log.append(f"🔴 SOLD {sym}")
                continue
            cur_val, want = pos["quantity"] * price, equity * w
            if want > 0 and cur_val > want * (1 + BAND):
                frac = 1.0 - want / cur_val
                if await self._sell(sym, price, frac, "Resize (risk scaling)"):
                    log.append(f"➖ TRIMMED {sym}")

        # 2) entries / top-ups with available cash
        positions = {p["symbol"]: p for p in await self.db.get_portfolio_positions(self.user_id)}
        capital = float(self.user.get("capital", 0.0))
        cash = capital - sum(p["entry_cost"] for p in positions.values())
        for sym, w in sorted(weights.items(), key=lambda kv: -kv[1]):
            price = prices.get(sym)
            if not price or w <= 0:
                continue
            want = equity * w
            have = positions[sym]["quantity"] * price if sym in positions else 0.0
            if sym in positions and have >= want * (1 - BAND):
                continue
            spend = min(want - have, cash * 0.98)
            if not self.exchange.is_paper:
                free = await self.exchange.get_usdt_balance()
                if free < spend and Config.ENABLE_EARN and float(self.user.get("earn_principal") or 0) > 0:
                    if await self._earn_redeem(min(spend - free + 1.0, float(self.user["earn_principal"]))):
                        free = await self.exchange.get_usdt_balance()
                spend = min(spend, free * 0.98)
            if spend < Config.MIN_ORDER_USDT:
                continue
            if await self._buy(sym, price, spend):
                cash -= spend
                log.append(f"🟢 {'ADDED' if sym in positions else 'BOUGHT'} {sym} ${spend:.2f}")

        await self.db.update_user(self.user_id, last_rebalance=datetime.utcnow().date().isoformat())
        self.user["last_rebalance"] = datetime.utcnow().date().isoformat()

        positions = await self.db.get_portfolio_positions(self.user_id)
        eq = await self._equity(prices)
        invested = sum(p["quantity"] * prices.get(p["symbol"], p["entry_price"]) for p in positions)
        mode = "PAPER" if self.exchange.is_paper else "LIVE"
        held = ", ".join(p["symbol"].replace("USDT", "") for p in positions) or "none (all cash)"
        await self._notify(
            f"📊 WEEKLY REBALANCE ({mode})\n"
            + ("\n".join(log) if log else "No changes needed") + "\n\n"
            f"Holding: {held}\n"
            f"Invested ${invested:.2f} of ${eq:.2f} ({(invested / eq * 100) if eq else 0:.0f}%)\n"
            f"Risk scale: {tgt['scale']:.2f} | In uptrend: {len(tgt['eligible'])}/{len(Config.PORTFOLIO_COINS)} coins"
            + ("\n\n⚠️ Capital is small: positions under Binance's ~$10 minimum are skipped. "
               "Use /setcapital with a larger amount (about $300+ recommended)."
               if float(self.user.get("capital", 0)) < 300 else "")
        )
        return True

    # ------------------------------------------------------------------ orders
    async def _buy(self, sym: str, price: float, usdt: float) -> bool:
        try:
            base, quote = await self.exchange.split_symbol(sym)
            existing = next((p for p in await self.db.get_portfolio_positions(self.user_id) if p["symbol"] == sym), None)
            if existing and existing.get("stop_order_id"):
                if not await self.exchange.cancel_order(sym, existing["stop_order_id"]):
                    return False
            order = await self.exchange.place_market_buy(sym, usdt_amount=usdt)
            if not order:
                if existing:  # restore protection
                    sid = await self.exchange.place_stop_order(sym, existing["quantity"], existing["stop_price"])
                    existing["stop_order_id"] = sid or ""
                    await self.db.save_portfolio_position(self.user_id, existing)
                return False
            fill = parse_fill(order, base, quote, Config.FEE_RATE)
            if fill["qty"] <= 0:
                return False
            qty = fill["qty"] + (existing["quantity"] if existing else 0.0)
            cost = fill["quote"] + fill["fee_quote"] + (existing["entry_cost"] if existing else 0.0)
            entry = cost / qty if qty else price
            stop_price = entry * (1 - Config.DISASTER_STOP_PCT / 100.0)
            sid = await self.exchange.place_stop_order(sym, qty, stop_price)
            pos = {"symbol": sym, "entry_price": entry, "quantity": qty, "entry_cost": cost,
                   "stop_price": stop_price, "stop_order_id": sid or "",
                   "entry_time": existing["entry_time"] if existing else datetime.utcnow().isoformat()}
            await self.db.save_portfolio_position(self.user_id, pos)
            if not sid:  # never hold live without crash protection
                logger.error(f"User {self.user_id}: stop failed for {sym}; flattening")
                await self._sell(sym, price, 1.0, "Protective stop could not be placed")
                return False
            return True
        except Exception as e:
            logger.error(f"Buy error {sym}: {e}", exc_info=True)
            return False

    async def _sell(self, sym: str, price: float, fraction: float, reason: str) -> bool:
        """Sell `fraction` (0-1] of a position; books realised PnL net of fees."""
        try:
            pos = next((p for p in await self.db.get_portfolio_positions(self.user_id) if p["symbol"] == sym), None)
            if not pos:
                return False
            if pos.get("stop_order_id"):
                if not await self.exchange.cancel_order(sym, pos["stop_order_id"]):
                    await self._reconcile()  # it may have just filled
                    return False
            sell_qty = pos["quantity"] * min(fraction, 1.0)
            order = await self.exchange.place_market_sell(sym, quantity=sell_qty)
            if not order:
                sid = await self.exchange.place_stop_order(sym, pos["quantity"], pos["stop_price"])
                pos["stop_order_id"] = sid or ""
                await self.db.save_portfolio_position(self.user_id, pos)
                return False
            base, quote = await self.exchange.split_symbol(sym)
            fill = parse_fill(order, base, quote, Config.FEE_RATE)
            await self._book(pos, fill["avg_price"] or price, fill["quote"] - fill["fee_quote"],
                             min(fraction, 1.0), reason)
            return True
        except Exception as e:
            logger.error(f"Sell error {sym}: {e}", exc_info=True)
            return False

    async def _book(self, pos: dict, exit_price: float, proceeds: float, fraction: float, reason: str) -> None:
        cost_part = pos["entry_cost"] * fraction
        pnl = proceeds - cost_part
        await self.db.save_trade({
            "user_id": self.user_id, "symbol": pos["symbol"], "side": "BUY",
            "entry_price": pos["entry_price"], "exit_price": exit_price,
            "quantity": pos["quantity"] * fraction, "pnl": round(pnl, 4),
            "pnl_percent": round(pnl / cost_part * 100.0, 2) if cost_part > 0 else 0.0,
            "entry_time": pos["entry_time"], "exit_time": datetime.utcnow().isoformat(), "reason": reason,
        })
        capital = float(self.user.get("capital", 0.0))
        new_cap = max(0.0, capital + pnl)
        await self.db.update_user(self.user_id, capital=round(new_cap, 2))
        self.user["capital"] = new_cap

        if fraction >= 0.999:
            await self.db.delete_portfolio_position(self.user_id, pos["symbol"])
        else:
            keep = 1.0 - fraction
            rest = dict(pos, quantity=pos["quantity"] * keep, entry_cost=pos["entry_cost"] * keep)
            rest["stop_order_id"] = await self.exchange.place_stop_order(pos["symbol"], rest["quantity"], pos["stop_price"]) or ""
            await self.db.save_portfolio_position(self.user_id, rest)
        sign = "+" if pnl >= 0 else ""
        await self._notify(f"{'🟢' if pnl >= 0 else '🔴'} {pos['symbol']} {reason}\n"
                           f"Net PnL: {sign}${pnl:.2f} | Capital: ${new_cap:.2f}")

    # ------------------------------------------------------------------ idle-cash yield (optional)
    async def _earn_sync_income(self) -> None:
        """Interest = growth of the Earn balance beyond what we put in. Book it as income, not as trading PnL."""
        try:
            bal = await self.exchange.earn_balance()
            principal = float(self.user.get("earn_principal") or 0)
            if bal is None or principal <= 0:
                return
            income = bal - principal
            if income >= 0.01:
                capital = float(self.user.get("capital", 0)) + income
                total_income = float(self.user.get("earn_income") or 0) + income
                await self.db.update_user(self.user_id, capital=round(capital, 4),
                                          earn_income=round(total_income, 4), earn_principal=round(bal, 4))
                self.user.update(capital=capital, earn_income=total_income, earn_principal=bal)
        except Exception as e:
            logger.warning(f"Earn income sync failed for user {self.user_id}: {e}")

    async def _earn_sweep(self, prices: Dict[str, float]) -> None:
        """Move the bot's OWN idle cash (never other funds in the account) into Flexible Earn."""
        try:
            positions = await self.db.get_portfolio_positions(self.user_id)
            equity = await self._equity(prices)
            capital = float(self.user.get("capital", 0))
            principal = float(self.user.get("earn_principal") or 0)
            bot_idle = capital - sum(p["entry_cost"] for p in positions) - principal
            buffer = max(Config.EARN_MIN_BUFFER_USDT, equity * Config.EARN_BUFFER_PCT / 100.0)
            spot_free = await self.exchange.get_usdt_balance()
            amount = min(spot_free - buffer, bot_idle - buffer)
            if amount < max(Config.MIN_ORDER_USDT, 10.0):
                return
            product = await self.exchange.earn_product()
            if not product:
                return
            if amount < float(product.get("minPurchaseAmount") or 0):
                return
            if await self.exchange.earn_subscribe(product["productId"], amount):
                new_p = principal + amount
                await self.db.update_user(self.user_id, earn_principal=round(new_p, 4))
                self.user["earn_principal"] = new_p
                apr = float(product.get("latestAnnualPercentageRate") or 0) * 100
                await self._notify(f"💤➡️💰 Moved ${amount:.2f} of idle cash to Binance Simple Earn "
                                   f"(advertised rate about {apr:.1f}%/yr; rates change and are not guaranteed).")
        except Exception as e:
            logger.warning(f"Earn sweep failed for user {self.user_id}: {e}")

    async def _earn_redeem(self, amount: float) -> bool:
        try:
            product = await self.exchange.earn_product()
            if not product or amount <= 0:
                return False
            if not await self.exchange.earn_redeem(product["productId"], amount):
                return False
            principal = max(0.0, float(self.user.get("earn_principal") or 0) - amount)
            await self.db.update_user(self.user_id, earn_principal=round(principal, 4))
            self.user["earn_principal"] = principal
            return True
        except Exception as e:
            logger.warning(f"Earn redeem failed for user {self.user_id}: {e}")
            return False

    # ------------------------------------------------------------------ safety nets
    async def _check_disaster_stops(self, prices: Dict[str, float]) -> None:
        """Paper mode (and live backup): sell any position that fell through its crash stop."""
        for pos in await self.db.get_portfolio_positions(self.user_id):
            px = prices.get(pos["symbol"])
            if px and px <= pos["stop_price"]:
                await self._sell(pos["symbol"], px, 1.0, f"Crash stop hit ({Config.DISASTER_STOP_PCT:g}%)")

    async def _reconcile(self) -> None:
        """Live: make the database agree with Binance (stop fills, cancelled stops, manual sales)."""
        try:
            for pos in await self.db.get_portfolio_positions(self.user_id):
                sym, sid = pos["symbol"], pos.get("stop_order_id") or ""
                if sid:
                    order = await self.exchange.get_order(sym, sid)
                    status = (order or {}).get("status")
                    if status == "FILLED":
                        qty = float(order.get("executedQty") or 0)
                        quote = float(order.get("cummulativeQuoteQty") or 0)
                        px = quote / qty if qty else pos["stop_price"]
                        await self._book(pos, px, quote, 1.0, "Exchange crash-stop filled")
                        continue
                    if status in ("CANCELED", "EXPIRED", "REJECTED"):
                        new_id = await self.exchange.place_stop_order(sym, pos["quantity"], pos["stop_price"])
                        if new_id:
                            pos["stop_order_id"] = new_id
                            await self.db.save_portfolio_position(self.user_id, pos)
                        else:
                            px = await self.exchange.get_price(sym) or pos["entry_price"]
                            pos["stop_order_id"] = ""
                            await self.db.save_portfolio_position(self.user_id, pos)
                            await self._sell(sym, px, 1.0, "Protective stop lost and could not be re-armed")
                            continue
                base, _ = await self.exchange.split_symbol(sym)
                held = await self.exchange.get_asset_total(base)
                if held is not None and held < pos["quantity"] * 0.9:
                    px = await self.exchange.get_price(sym) or pos["entry_price"]
                    if sid:
                        await self.exchange.cancel_order(sym, sid)
                    await self._book(pos, px, px * pos["quantity"] * (1 - Config.FEE_RATE), 1.0,
                                     "Closed outside the bot (estimated price)")
        except Exception as e:
            logger.error(f"Portfolio reconcile error for user {self.user_id}: {e}", exc_info=True)
