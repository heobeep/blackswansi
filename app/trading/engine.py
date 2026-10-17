import asyncio
import logging
from datetime import datetime, timedelta
from typing import Callable, Optional, List, Dict, Any

from app.database import Database
from app.config import Config
from app.trading.strategy import analyze, SignalResult, StrategyParams
from app.trading.exchange import Exchange
from app.trading.risk import position_size, parse_fill, ratchet_stop

logger = logging.getLogger(__name__)


class TradingEngine:
    def __init__(self, user_id: int, db: Database, notifier: Callable):
        self.user_id = user_id
        self.db = db
        self.notifier = notifier  # async callable(user_id, message)
        self.user: Dict[str, Any] = {}
        self.exchange: Optional[Exchange] = None
        self._cycle_count = 0
        self.params = StrategyParams(trend_ema=Config.TREND_EMA)

    async def initialize(self) -> None:
        """Load user from DB and initialize Exchange with credentials."""
        try:
            user = await self.db.get_user(self.user_id)
            if not user:
                logger.warning(f"Engine init: User {self.user_id} not found in DB")
                self.user = {}
            else:
                self.user = user

            api_key = self.user.get("binance_api_key", "")
            secret_key = self.user.get("binance_secret_key", "")
            self.exchange = Exchange(api_key=api_key, secret_key=secret_key)
            logger.info(f"TradingEngine initialized for user {self.user_id} (paper={self.exchange.is_paper})")
        except Exception as e:
            logger.error(f"Failed to initialize TradingEngine for user {self.user_id}: {e}", exc_info=True)

    async def run_once(self) -> str:
        """
        Execute a full trading cycle:
        1. Reload user config from DB
        2. Fetch current price
        3. Fetch klines
        4. Check SL/TP on open position
        5. If position wasn't closed, evaluate trading strategy
        6. Open long if BUY signal and no position
        7. Close position if SELL signal and open position
        8. Record portfolio snapshot every 4th cycle
        """
        try:
            # 1. Reload user config from DB
            user = await self.db.get_user(self.user_id)
            if not user:
                return f"User {self.user_id} not found"
            self.user = user

            if not self.user.get("is_trading"):
                return f"Trading stopped for user {self.user_id}"

            pair = self.user.get("trading_pair", "BTCUSDT")
            interval = self.user.get("candle_interval", "15m")

            if self.exchange is None:
                api_key = self.user.get("binance_api_key", "")
                secret_key = self.user.get("binance_secret_key", "")
                self.exchange = Exchange(api_key=api_key, secret_key=secret_key)

            # 1b. Live mode: make sure our books match what is really on Binance
            if not self.exchange.is_paper:
                await self._reconcile_live()

            # 2. Get current price
            current_price = await self.exchange.get_price(pair)
            if current_price is None or current_price <= 0:
                return f"Failed to get price for {pair}"

            # 3. Check open position for SL/TP hits first
            position_closed = await self._check_sl_tp(current_price)
            if not position_closed:
                await self._update_trailing(current_price)

            # 4. Fetch klines
            df = await self.exchange.get_klines(pair, interval=interval, limit=max(500, self.params.min_rows() + 50))
            if df is None or df.empty:
                return f"Failed to get klines for {pair}"

            # 5. Run strategy analysis
            signal_result: SignalResult = analyze(df, self.params)

            # 6 & 7. Check open position and execute signals
            open_position = await self.db.get_open_position(self.user_id)
            action = "HOLD"

            if signal_result.signal == "BUY" and open_position is None:
                blocked = await self._entry_blocked(interval)
                if blocked:
                    logger.info(f"User {self.user_id}: BUY skipped ({blocked})")
                    action = f"SKIP ({blocked})"
                else:
                    await self._open_long(current_price, signal_result.reasons)
                    action = "BUY"
            elif signal_result.signal == "SELL" and open_position is not None:
                await self._close_position(current_price, "Strategy SELL signal")
                action = "SELL"

            # 8. Record snapshot every 4th cycle
            self._cycle_count += 1
            if self._cycle_count % 4 == 0:
                current_open_pos = await self.db.get_open_position(self.user_id)
                capital = float(self.user.get("capital", 100.0))
                if current_open_pos:
                    entry_cap = float(current_open_pos.get("entry_capital", 0.0))
                    current_val = float(current_open_pos["quantity"]) * current_price
                    unrealized_pnl = current_val - entry_cap
                    equity = capital + unrealized_pnl
                else:
                    equity = capital
                await self.db.save_snapshot(self.user_id, equity=round(equity, 4), price=current_price)

            status_msg = (
                f"User {self.user_id} | {pair} @ ${current_price:.4f} | "
                f"Signal: {signal_result.signal} | Action: {action}"
            )
            logger.info(status_msg)
            return status_msg
        except Exception as e:
            logger.error(f"Error in run_once for user {self.user_id}: {e}", exc_info=True)
            return f"Error: {e}"

    async def _check_sl_tp(self, current_price: float) -> bool:
        """Check if open position hit stop loss or take profit. Returns True if closed."""
        try:
            position = await self.db.get_open_position(self.user_id)
            if not position:
                return False

            stop_loss = float(position["stop_loss"])
            take_profit = float(position["take_profit"])

            if current_price <= stop_loss:
                reason = f"Stop Loss hit (${current_price:.4f} <= ${stop_loss:.4f})"
                logger.info(f"SL triggered for user {self.user_id}: {reason}")
                await self._close_position(current_price, reason)
                return True
            elif current_price >= take_profit:
                reason = f"Take Profit hit (${current_price:.4f} >= ${take_profit:.4f})"
                logger.info(f"TP triggered for user {self.user_id}: {reason}")
                await self._close_position(current_price, reason)
                return True

            return False
        except Exception as e:
            logger.error(f"Error checking SL/TP for user {self.user_id}: {e}", exc_info=True)
            return False

    async def _update_trailing(self, price: float) -> None:
        """Track the high-water mark and ratchet the stop up (never down); mirror it on the exchange."""
        position = await self.db.get_open_position(self.user_id)
        if not position:
            return
        peak = max(float(position.get("peak_price") or 0.0), price)
        sl_pct = float(self.user.get("stop_loss_percent", 2.0))
        old_stop = float(position["stop_loss"])
        new_stop = ratchet_stop(float(position["entry_price"]), peak, old_stop, sl_pct, Config.FEE_RATE)

        if new_stop < old_stop * 1.002:  # ignore dust moves (saves API calls)
            if peak > float(position.get("peak_price") or 0.0):
                await self.db.update_position(self.user_id, peak_price=peak)
            return

        pair = position["symbol"]
        new_id = position.get("stop_order_id") or ""
        if not self.exchange.is_paper and new_id:
            if not await self.exchange.cancel_order(pair, new_id):
                return  # leave things as they are; reconcile will handle a filled stop
            new_id = await self.exchange.place_stop_order(pair, float(position["quantity"]), new_stop)
            if not new_id:
                # tighten failed: fall back to the old level so we are never unprotected
                new_id = await self.exchange.place_stop_order(pair, float(position["quantity"]), old_stop) or ""
                await self.db.update_position(self.user_id, stop_order_id=new_id, peak_price=peak)
                return
        await self.db.update_position(self.user_id, stop_loss=new_stop, stop_order_id=new_id, peak_price=peak)
        logger.info(f"User {self.user_id}: stop raised {old_stop:.4f} -> {new_stop:.4f}")

    async def _book_external_close(self, position: dict, exit_price: float, reason: str) -> None:
        """Record a position that was closed on the exchange (stop fired, or user sold manually)."""
        qty = float(position["quantity"])
        cost = float(position["entry_capital"])
        proceeds = exit_price * qty * (1 - Config.FEE_RATE)
        pnl = proceeds - cost
        pnl_pct = (pnl / cost) * 100.0 if cost > 0 else 0.0
        await self.db.save_trade({
            "user_id": self.user_id, "symbol": position["symbol"], "side": "BUY",
            "entry_price": float(position["entry_price"]), "exit_price": exit_price, "quantity": qty,
            "pnl": round(pnl, 4), "pnl_percent": round(pnl_pct, 2),
            "entry_time": position["entry_time"], "exit_time": datetime.utcnow().isoformat(), "reason": reason,
        })
        await self.db.clear_position(self.user_id)
        capital = float(self.user.get("capital", 100.0))
        new_capital = max(0.0, capital + pnl)
        await self.db.update_user(self.user_id, capital=round(new_capital, 2))
        self.user["capital"] = new_capital
        sign = "+" if pnl >= 0 else ""
        await self._notify(
            f"{'🟢' if pnl >= 0 else '🔴'} POSITION CLOSED\n"
            f"💰 {position['symbol']} @ ${exit_price:.4f}\n"
            f"📊 {reason}\n"
            f"💵 Net PnL: {sign}${pnl:.2f} ({sign}{pnl_pct:.2f}%)\n"
            f"💼 Capital: ${new_capital:.2f}"
        )

    async def _reconcile_live(self) -> None:
        """
        Make the database agree with Binance (the source of truth):
          1. exchange stop FILLED          -> book the trade
          2. exchange stop gone/cancelled  -> re-arm it, or flatten if that fails
          3. coins no longer in the wallet -> user sold manually; close our record
        """
        try:
            position = await self.db.get_open_position(self.user_id)
            if not position:
                return
            pair = position["symbol"]
            stop_id = position.get("stop_order_id") or ""

            if stop_id:
                order = await self.exchange.get_order(pair, stop_id)
                status = (order or {}).get("status")
                if status == "FILLED":
                    qty = float(order.get("executedQty") or 0)
                    quote = float(order.get("cummulativeQuoteQty") or 0)
                    px = quote / qty if qty else float(position["stop_loss"])
                    await self._book_external_close(position, px, "Exchange stop-loss filled")
                    return
                if status in ("CANCELED", "EXPIRED", "REJECTED"):
                    logger.warning(f"User {self.user_id}: stop order {stop_id} is {status}; re-arming")
                    new_id = await self.exchange.place_stop_order(pair, float(position["quantity"]),
                                                                  float(position["stop_loss"]))
                    if new_id:
                        await self.db.update_position(self.user_id, stop_order_id=new_id)
                    else:
                        price = await self.exchange.get_price(pair) or float(position["entry_price"])
                        await self.db.update_position(self.user_id, stop_order_id="")
                        await self._close_position(price, "Protective stop lost and could not be re-armed")
                        return

            base, _ = await self.exchange.split_symbol(pair)
            held = await self.exchange.get_asset_total(base)
            if held is not None and held < float(position["quantity"]) * 0.9:
                price = await self.exchange.get_price(pair) or float(position["entry_price"])
                if stop_id:
                    await self.exchange.cancel_order(pair, stop_id)
                await self._book_external_close(position, price, "Closed outside the bot (estimated price)")
        except Exception as e:
            logger.error(f"Reconcile error for user {self.user_id}: {e}", exc_info=True)

    async def _entry_blocked(self, interval: str) -> Optional[str]:
        """Return a reason string if new entries must be skipped right now."""
        capital = float(self.user.get("capital", 100.0))

        # Daily loss limit (UTC day)
        day_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        pnl_today = await self.db.get_realized_pnl_since(self.user_id, day_start)
        if capital > 0 and pnl_today <= -capital * (Config.MAX_DAILY_LOSS_PCT / 100.0):
            return f"daily loss limit {Config.MAX_DAILY_LOSS_PCT:g}% reached"

        # Cooldown after any exit, to avoid whipsaw re-entries
        last_exit = await self.db.get_last_exit_time(self.user_id)
        if last_exit and Config.COOLDOWN_CANDLES > 0:
            secs = Config.INTERVAL_SECONDS.get(interval, 900) * Config.COOLDOWN_CANDLES
            try:
                if datetime.utcnow() - datetime.fromisoformat(last_exit) < timedelta(seconds=secs):
                    return "cooldown after last exit"
            except ValueError:
                pass
        return None

    async def _open_long(self, price: float, reasons: List[str]) -> None:
        """Execute buy order and record open position using REAL fill data."""
        try:
            capital = float(self.user.get("capital", 100.0))
            risk_percent = float(self.user.get("risk_percent", 1.0))
            sl_percent = float(self.user.get("stop_loss_percent", 2.0))
            tp_percent = float(self.user.get("take_profit_percent", 4.0))
            pair = self.user.get("trading_pair", "BTCUSDT")

            allocated = position_size(capital, risk_percent, sl_percent, Config.MAX_POSITION_PCT)
            if not self.exchange.is_paper:
                # 'capital' is a bookkeeping number; the real limit is what is actually on Binance.
                free = await self.exchange.get_usdt_balance()
                if free < allocated:
                    allocated = free * 0.98
                    logger.warning(f"User {self.user_id}: USDT balance ${free:.2f} below target size; capping")
            if allocated < 10.0 or price <= 0:  # Binance min notional is ~5-10 USDT
                logger.warning(f"Allocation too small/invalid ({allocated:.2f}) for user {self.user_id}")
                return

            order = await self.exchange.place_market_buy(pair, usdt_amount=allocated)
            if not order:
                logger.error(f"Buy order execution failed on exchange for user {self.user_id}")
                return

            base, quote = await self.exchange.split_symbol(pair)
            fill = parse_fill(order, base, quote, Config.FEE_RATE)
            if fill["qty"] <= 0:
                logger.error(f"Buy returned zero quantity for user {self.user_id}: {order}")
                return
            entry_price = fill["avg_price"] or price
            quantity = fill["qty"]
            cost = fill["quote"] + fill["fee_quote"]

            stop_loss = entry_price * (1.0 - sl_percent / 100.0)
            take_profit = entry_price * (1.0 + tp_percent / 100.0)

            # Protective stop lives ON the exchange. Live mode never runs unprotected.
            stop_id = await self.exchange.place_stop_order(pair, quantity, stop_loss)
            if not stop_id:
                logger.error(f"User {self.user_id}: protective stop failed - flattening position")
                await self.db.save_position(self.user_id, {
                    "symbol": pair, "entry_price": entry_price, "quantity": quantity,
                    "stop_loss": stop_loss, "take_profit": take_profit,
                    "entry_time": datetime.utcnow().isoformat(), "entry_capital": cost,
                    "peak_price": entry_price,
                })
                await self._close_position(entry_price, "Protective stop could not be placed")
                return

            await self.db.save_position(self.user_id, {
                "symbol": pair,
                "entry_price": entry_price,
                "quantity": quantity,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
                "entry_time": datetime.utcnow().isoformat(),
                "entry_capital": cost,
                "stop_order_id": stop_id,
                "peak_price": entry_price,
            })

            mode = "PAPER" if self.exchange.is_paper else "LIVE"
            reason_text = reasons[0] if reasons else "Multi-indicator confirmation"
            msg = (
                f"🟢 BUY EXECUTED ({mode})\n"
                f"💰 {pair} @ ${entry_price:.4f}\n"
                f"📊 Qty: {quantity:.6f} | Cost: ${cost:.2f} (fees ${fill['fee_quote']:.2f})\n"
                f"🛡 SL: ${stop_loss:.4f} | TP: ${take_profit:.4f}\n"
                f"✨ {reason_text}"
            )
            await self._notify(msg)
        except Exception as e:
            logger.error(f"Error opening long position for user {self.user_id}: {e}", exc_info=True)

    async def _close_position(self, price: float, reason: str) -> None:
        """Execute sell order, compute net PnL (after fees), record trade, update capital."""
        try:
            position = await self.db.get_open_position(self.user_id)
            if not position:
                return

            pair = position["symbol"]
            quantity = float(position["quantity"])
            entry_price = float(position["entry_price"])
            cost = float(position["entry_capital"])

            stop_id = position.get("stop_order_id") or ""
            if stop_id and not self.exchange.is_paper:
                if not await self.exchange.cancel_order(pair, stop_id):
                    # Could not cancel: it may have just filled. Let reconciliation sort it out.
                    logger.warning(f"User {self.user_id}: stop cancel failed, deferring to reconcile")
                    await self._reconcile_live()
                    return

            order = await self.exchange.place_market_sell(pair, quantity=quantity)
            if not order:
                logger.error(f"Sell order execution failed on exchange for user {self.user_id}")
                if stop_id and not self.exchange.is_paper:  # restore protection
                    new_id = await self.exchange.place_stop_order(pair, quantity, float(position["stop_loss"]))
                    await self.db.update_position(self.user_id, stop_order_id=new_id or "")
                return

            base, quote = await self.exchange.split_symbol(pair)
            fill = parse_fill(order, base, quote, Config.FEE_RATE)
            exit_price = fill["avg_price"] or price
            proceeds = fill["quote"] - fill["fee_quote"]
            pnl = proceeds - cost
            pnl_percent = (pnl / cost) * 100.0 if cost > 0 else 0.0

            await self.db.save_trade({
                "user_id": self.user_id,
                "symbol": pair,
                "side": "BUY",
                "entry_price": entry_price,
                "exit_price": exit_price,
                "quantity": quantity,
                "pnl": round(pnl, 4),
                "pnl_percent": round(pnl_percent, 2),
                "entry_time": position["entry_time"],
                "exit_time": datetime.utcnow().isoformat(),
                "reason": reason,
            })
            await self.db.clear_position(self.user_id)

            capital = float(self.user.get("capital", 100.0))
            new_capital = max(0.0, capital + pnl)
            await self.db.update_user(self.user_id, capital=round(new_capital, 2))
            self.user["capital"] = new_capital

            emoji = "🟢" if pnl >= 0 else "🔴"
            sign = "+" if pnl >= 0 else ""
            await self._notify(
                f"{emoji} POSITION CLOSED\n"
                f"💰 {pair} @ ${exit_price:.4f}\n"
                f"📊 Reason: {reason}\n"
                f"💵 Net PnL: {sign}${pnl:.2f} ({sign}{pnl_percent:.2f}%) after fees\n"
                f"💼 Capital: ${new_capital:.2f}"
            )
        except Exception as e:
            logger.error(f"Error closing position for user {self.user_id}: {e}", exc_info=True)

    async def _notify(self, msg: str) -> None:
        if self.notifier:
            try:
                await self.notifier(self.user_id, msg)
            except Exception as ne:
                logger.error(f"Notifier error: {ne}")

    async def close(self) -> None:
        """Close exchange connections."""
        if self.exchange:
            try:
                await self.exchange.close()
            except Exception as e:
                logger.error(f"Error closing exchange for user {self.user_id}: {e}")
