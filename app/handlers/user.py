import logging
from datetime import datetime
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import ContextTypes

from app.config import Config
from app.database import Database
from app.scheduler import Scheduler
from app.trading.exchange import Exchange

logger = logging.getLogger(__name__)

# Injected by main.py
db: Optional[Database] = None
scheduler_ref: Optional[Scheduler] = None

VALID_PAIRS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT", "DOGEUSDT"]
VALID_INTERVALS = ["1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "1d"]


async def is_authorized(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Middleware: Checks if a user is in the DB and has a valid subscription, OR is an admin.
    Also ensures user is upserted.
    """
    if not update.effective_user or not db:
        return False

    user_id = update.effective_user.id
    username = update.effective_user.username or ""
    first_name = update.effective_user.first_name or ""

    # Ensure user row exists in DB
    await db.upsert_user(user_id, username, first_name)

    # Admins are always authorized
    if user_id in Config.ADMIN_IDS:
        return True

    user = await db.get_user(user_id)
    if not user:
        if update.effective_message:
            await update.effective_message.reply_text(
                "❌ You are not registered. Contact admin to get access."
            )
        return False

    if user.get("is_admin") == 1:
        return True

    # Check active status & subscription validity
    if user.get("is_active") != 1:
        if update.effective_message:
            await update.effective_message.reply_text(
                f"⏳ <b>Account Pending Activation</b>\n\n"
                f"Your Telegram ID: <code>{user_id}</code>\n"
                f"Please share this ID with the admin to activate your access.",
                parse_mode="HTML"
            )
        return False

    is_valid = await db.is_subscription_valid(user_id)
    if not is_valid:
        if update.effective_message:
            await update.effective_message.reply_text(
                f"❌ <b>Subscription Expired</b>\n\n"
                f"Your Telegram ID: <code>{user_id}</code>\n"
                f"Your subscription has expired. Please contact the admin to renew.",
                parse_mode="HTML"
            )
        return False

    return True


def get_main_menu_keyboard() -> InlineKeyboardMarkup:
    """Build the main interactive inline keyboard."""
    webapp_url = f"{Config.WEBHOOK_URL.rstrip('/')}/webapp" if Config.WEBHOOK_URL else "https://telegram.org"

    portfolio_btn = (
        InlineKeyboardButton("💼 Open Live Portfolio", web_app=WebAppInfo(url=webapp_url))
        if Config.WEBHOOK_URL
        else InlineKeyboardButton("💼 Portfolio Summary", callback_data="cb_portfolio")
    )

    keyboard = [
        [
            InlineKeyboardButton("📊 Status", callback_data="cb_status"),
            InlineKeyboardButton("⚙️ Settings", callback_data="cb_settings"),
        ],
        [
            InlineKeyboardButton("📈 Start Trading", callback_data="cb_start_trading"),
            InlineKeyboardButton("📋 History", callback_data="cb_history"),
        ],
        [
            portfolio_btn
        ]
    ]
    return InlineKeyboardMarkup(keyboard)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start command. Show welcome or activation request."""
    if not update.effective_user or not db:
        return

    user_id = update.effective_user.id
    username = update.effective_user.username or ""
    first_name = update.effective_user.first_name or "Trader"

    # Always upsert user
    await db.upsert_user(user_id, username, first_name)

    # Check if authorized
    authorized = await is_authorized(update, context)
    if not authorized:
        return

    welcome_text = (
        f"👋 <b>Welcome, {first_name}!</b>\n\n"
        "Welcome to <b>BlackSwanSI</b>, a rule-based crypto trading assistant.\n\n"
        "• <b>Strategy:</b> daily trend-following across 10 large coins; holds cash in downtrends\n"
        "• <b>Modes:</b> Paper (simulated money, the default) and Binance spot\n"
        "• <b>Honest note:</b> it aims to limit crashes, it does <b>not</b> guarantee profit. "
        "Losing periods happen.\n\n"
        "Tap a button below to get started or type /help for commands:"
    )

    await update.effective_message.reply_text(
        welcome_text,
        reply_markup=get_main_menu_keyboard(),
        parse_mode="HTML"
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /help command. Show detailed commands listing."""
    if not await is_authorized(update, context):
        return

    user_id = update.effective_user.id
    is_admin = user_id in Config.ADMIN_IDS

    lines = [
        "📖 <b>BlackSwanSI — Command Guide</b>\n",
        "<b>Trading Controls:</b>",
        "• <code>/start</code> — Open main control dashboard",
        "• <code>/status</code> — Current trading state",
        "• <code>/holdings</code> — Coins currently held and cash",
        "• <code>/starttrade</code> — Start the automated strategy",
        "• <code>/stoptrade</code> — Stop it",
        "• <code>/balance</code> — Check exchange balance or paper capital",
        "• <code>/portfolio</code> — Open live WebApp portfolio dashboard",
        "• <code>/history</code> — View recent trade logs & performance stats\n",
        "<b>Configuration:</b>",
        "• <code>/setkeys &lt;api_key&gt; &lt;secret_key&gt;</code> — Set Binance API credentials (auto-deleted)",
        "• <code>/setcapital &lt;amount&gt;</code> — Set trading capital in USDT ($300+ recommended)",
        "• <code>/setrisk conservative|balanced|growth</code> — More risk = more potential profit AND bigger drops",
        "• <code>/setmode portfolio|single</code> — Strategy (portfolio is the tested one)",
        "• <code>/setearn on|off</code> — Idle-cash interest (live accounts, if enabled)",
        "• <code>/setpair</code>, <code>/setinterval</code> — only used by the older single-pair mode\n",
    ]

    if is_admin:
        lines.extend([
            "👑 <b>Admin Commands:</b>",
            "• <code>/admin</code> — Admin management panel",
            "• <code>/adduser &lt;id&gt; &lt;days&gt;</code> — Activate user subscription",
            "• <code>/removeuser &lt;id&gt;</code> — Deactivate user and halt trading",
            "• <code>/listusers</code> — List all registered traders",
            "• <code>/globalstats</code> — System uptime & active trader counts",
            "• <code>/broadcast &lt;message&gt;</code> — Send announcement to all active users",
            "• <code>/setlimit &lt;id&gt; &lt;capital&gt;</code> — Override user capital limit\n",
        ])

    lines.append("<i>Need support? Contact your administrator.</i>")
    await update.effective_message.reply_text("\n".join(lines), parse_mode="HTML")


async def set_keys(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle /setkeys command.
    Usage: /setkeys <api_key> <secret_key>
    Deletes the source message immediately for security, then validates with Binance.
    """
    if not await is_authorized(update, context):
        return

    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    message_id = update.effective_message.message_id

    # IMMEDIATELY delete the user's message containing raw keys
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception as e:
        logger.warning(f"Failed to delete sensitive message {message_id}: {e}")

    if not context.args or len(context.args) < 2:
        await update.effective_message.reply_text(
            "⚠️ <b>Usage:</b> <code>/setkeys &lt;api_key&gt; &lt;secret_key&gt;</code>\n\n"
            "<i>Your command was deleted immediately to protect your credentials.</i>\n"
            "Please re-run the command with both keys separated by a space.",
            parse_mode="HTML"
        )
        return

    api_key = context.args[0].strip()
    secret_key = context.args[1].strip()

    status_msg = await update.effective_message.reply_text(
        "🔄 <i>Validating credentials with Binance API...</i>",
        parse_mode="HTML"
    )

    if not Config.ENCRYPTION_KEY:
        await status_msg.edit_text(
            "❌ Server is not configured to store API keys securely (ENCRYPTION_KEY missing). "
            "Contact the administrator.",
            parse_mode="HTML",
        )
        return

    exchange = Exchange(api_key=api_key, secret_key=secret_key)
    safety_note = ""
    try:
        is_valid = await exchange.validate_keys()
        if is_valid:
            safe, safety_msg = await exchange.check_key_safety()
            if not safe:
                await status_msg.edit_text(f"❌ <b>Key rejected</b>\n\n{safety_msg}", parse_mode="HTML")
                return
            if safety_msg != "ok":
                safety_note = f"\n\n⚠️ {safety_msg}"
    except Exception as e:
        logger.error(f"Error during key validation for user {user_id}: {e}")
        is_valid = False
    finally:
        await exchange.close()

    if is_valid:
        await db.update_user(
            user_id,
            binance_api_key=api_key,
            binance_secret_key=secret_key
        )
        await status_msg.edit_text(
            "✅ <b>Binance API Keys Validated & Saved!</b>\n\n"
            + ("• Mode: <b>LIVE TRADING</b>\n" if Config.ALLOW_LIVE_TRADING else
               "• Mode: <b>PAPER</b> (live trading is disabled by the operator)\n")
            + "• Connection to Binance verified, withdrawals confirmed OFF\n"
            "• Credentials stored encrypted" + safety_note,
            parse_mode="HTML"
        )
        logger.info(f"User {user_id} configured valid Binance API keys")
    else:
        await status_msg.edit_text(
            "❌ <b>API Key Validation Failed</b>\n\n"
            "Binance rejected the credentials. Please verify that:\n"
            "1. The API Key and Secret are correct\n"
            "2. 'Enable Spot & Margin Trading' permission is enabled in Binance\n"
            "3. If IP restriction is enabled, ensure server IP is whitelisted",
            parse_mode="HTML"
        )


async def set_capital(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle /setcapital command.
    Usage: /setcapital <amount>
    Must be > 10 USDT.
    """
    if not await is_authorized(update, context):
        return

    user_id = update.effective_user.id
    if not context.args or len(context.args) < 1:
        await update.effective_message.reply_text(
            "ℹ️ <b>Usage:</b> <code>/setcapital &lt;amount&gt;</code>\n"
            "Example: <code>/setcapital 250</code>\n"
            "Minimum capital is 10.00 USDT.",
            parse_mode="HTML"
        )
        return

    try:
        amount = float(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("❌ <b>Error:</b> Amount must be a valid number.", parse_mode="HTML")
        return

    if amount < 10.0:
        await update.effective_message.reply_text(
            "❌ <b>Error:</b> Minimum trading capital is <b>10.00 USDT</b> (Binance minimum order limit).",
            parse_mode="HTML"
        )
        return

    await db.update_user(user_id, capital=amount)
    await update.effective_message.reply_text(
        f"✅ <b>Capital Updated</b>\n\nTrading capital set to <b>${amount:.2f} USDT</b>.",
        parse_mode="HTML"
    )
    logger.info(f"User {user_id} set capital to {amount}")


async def set_pair(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle /setpair command.
    Usage: /setpair <PAIR>
    """
    if not await is_authorized(update, context):
        return

    user_id = update.effective_user.id
    if not context.args or len(context.args) < 1:
        pairs_str = ", ".join(VALID_PAIRS)
        await update.effective_message.reply_text(
            f"ℹ️ <b>Usage:</b> <code>/setpair &lt;PAIR&gt;</code>\n"
            f"Supported pairs: <code>{pairs_str}</code>",
            parse_mode="HTML"
        )
        return

    pair = context.args[0].upper().strip()
    if pair not in VALID_PAIRS:
        pairs_str = ", ".join(VALID_PAIRS)
        await update.effective_message.reply_text(
            f"❌ <b>Invalid Pair:</b> <code>{pair}</code>\n"
            f"Please choose from: <code>{pairs_str}</code>",
            parse_mode="HTML"
        )
        return

    await db.update_user(user_id, trading_pair=pair)
    await update.effective_message.reply_text(
        f"✅ <b>Trading Pair Updated</b>\n\nActive pair set to <b>{pair}</b>.",
        parse_mode="HTML"
    )
    logger.info(f"User {user_id} set pair to {pair}")


async def set_interval(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle /setinterval command.
    Usage: /setinterval <interval>
    """
    if not await is_authorized(update, context):
        return

    user_id = update.effective_user.id
    if not context.args or len(context.args) < 1:
        intervals_str = ", ".join(VALID_INTERVALS)
        await update.effective_message.reply_text(
            f"ℹ️ <b>Usage:</b> <code>/setinterval &lt;interval&gt;</code>\n"
            f"Valid intervals: <code>{intervals_str}</code>",
            parse_mode="HTML"
        )
        return

    interval = context.args[0].lower().strip()
    if interval not in VALID_INTERVALS:
        intervals_str = ", ".join(VALID_INTERVALS)
        await update.effective_message.reply_text(
            f"❌ <b>Invalid Interval:</b> <code>{interval}</code>\n"
            f"Allowed values: <code>{intervals_str}</code>",
            parse_mode="HTML"
        )
        return

    await db.update_user(user_id, candle_interval=interval)
    await update.effective_message.reply_text(
        f"✅ <b>Candle Interval Updated</b>\n\nTimeframe set to <b>{interval}</b>.",
        parse_mode="HTML"
    )
    logger.info(f"User {user_id} set interval to {interval}")


async def start_trading(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start_trade or /starttrade command."""
    if not await is_authorized(update, context):
        return

    user_id = update.effective_user.id

    if scheduler_ref and scheduler_ref.is_running(user_id):
        await update.effective_message.reply_text(
            "⚠️ <b>Trading is already active!</b>\n"
            "Your bot is already analyzing candles in real-time. Use /status to inspect.",
            parse_mode="HTML"
        )
        return

    user = await db.get_user(user_id)
    if not user:
        await update.effective_message.reply_text("❌ User record not found.")
        return

    has_keys = Config.is_live(user)
    mode_str = "🚀 <b>LIVE TRADING (Binance Spot)</b>" if has_keys else "📝 <b>PAPER TRADING (Simulated)</b>"

    if scheduler_ref:
        await scheduler_ref.start_user(user_id)

    pair = user.get("trading_pair", "BTCUSDT")
    interval = user.get("candle_interval", "15m")
    capital = user.get("capital", 100.0)

    text = (
        f"🟢 <b>Trading Loop Started!</b>\n\n"
        f"• <b>Mode:</b> {mode_str}\n"
        f"• <b>Pair:</b> <code>{pair}</code>\n"
        f"• <b>Interval:</b> <code>{interval}</code>\n"
        f"• <b>Allocated Capital:</b> <b>${capital:.2f} USDT</b>\n"
        f"• <b>Risk / Trade:</b> {user.get('risk_percent', 2.0)}%\n"
        f"• <b>Stop Loss:</b> {user.get('stop_loss_percent', 2.0)}% | <b>Take Profit:</b> {user.get('take_profit_percent', 4.0)}%\n\n"
        "<i>You will receive instant alerts when buy/sell orders trigger.</i>"
    )

    await update.effective_message.reply_text(text, parse_mode="HTML")
    logger.info(f"User {user_id} started trading loop (Live={has_keys})")


async def stop_trading(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /stop_trade or /stoptrade command."""
    if not await is_authorized(update, context):
        return

    user_id = update.effective_user.id

    if scheduler_ref and not scheduler_ref.is_running(user_id):
        await update.effective_message.reply_text(
            "ℹ️ <b>Trading is currently stopped.</b> Use /start_trade to launch.",
            parse_mode="HTML"
        )
        return

    if scheduler_ref:
        await scheduler_ref.stop_user(user_id)

    await update.effective_message.reply_text(
        "🔴 <b>Automated Trading Stopped</b>\n\n"
        "The trading execution loop has been cleanly shut down.\n"
        "<i>Existing open positions remain untouched in the database.</i>",
        parse_mode="HTML"
    )
    logger.info(f"User {user_id} stopped trading loop")


async def format_status_message(user_id: int) -> str:
    """Format the full status message for a given user."""
    if not db:
        return "❌ Database not initialized."

    user = await db.get_user(user_id)
    if not user:
        return "❌ User profile not found."

    is_running = scheduler_ref.is_running(user_id) if scheduler_ref else False
    status_indicator = "🟢 <b>RUNNING</b>" if is_running else "🔴 <b>STOPPED</b>"

    has_keys = Config.is_live(user)
    mode_text = "🚀 LIVE (Binance)" if has_keys else "📝 PAPER (Simulated)"

    pair = user.get("trading_pair", "BTCUSDT")
    interval = user.get("candle_interval", "15m")
    capital = user.get("capital", 100.0)

    # Check open position
    pos = await db.get_open_position(user_id)
    pos_text = "<i>No open position (scanning for signals...)</i>"

    if pos:
        symbol = pos.get("symbol", pair)
        entry_price = float(pos.get("entry_price", 0.0))
        quantity = float(pos.get("quantity", 0.0))
        sl = float(pos.get("stop_loss", 0.0))
        tp = float(pos.get("take_profit", 0.0))

        # Fetch current real-time market price
        curr_price = None
        exchange = Exchange(
            api_key=user.get("binance_api_key", ""),
            secret_key=user.get("binance_secret_key", "")
        )
        try:
            curr_price = await exchange.get_price(symbol)
        except Exception:
            curr_price = None
        finally:
            await exchange.close()

        if curr_price and entry_price > 0:
            unrealized_pnl = (curr_price - entry_price) * quantity
            pnl_pct = ((curr_price - entry_price) / entry_price) * 100.0
            pnl_sign = "+" if unrealized_pnl >= 0 else ""
            pnl_emoji = "🟢" if unrealized_pnl >= 0 else "🔴"
            price_display = (
                f"• <b>Current Price:</b> ${curr_price:.4f}\n"
                f"• <b>Unrealized PnL:</b> {pnl_emoji} <b>{pnl_sign}${unrealized_pnl:.2f} ({pnl_sign}{pnl_pct:.2f}%)</b>\n"
            )
        else:
            price_display = "• <b>Current Price:</b> Fetching...\n"

        pos_text = (
            f"<b>Active Position:</b> <code>{symbol}</code>\n"
            f"• <b>Entry Price:</b> ${entry_price:.4f}\n"
            f"{price_display}"
            f"• <b>Quantity:</b> {quantity:.6f}\n"
            f"• <b>Stop Loss:</b> ${sl:.4f} | <b>Take Profit:</b> ${tp:.4f}"
        )

    return (
        "📊 <b>BlackSwanSI — System Status</b>\n\n"
        f"• <b>Trading State:</b> {status_indicator}\n"
        f"• <b>Execution Mode:</b> {mode_text}\n"
        f"• <b>Trading Pair:</b> <code>{pair}</code>\n"
        f"• <b>Timeframe:</b> <code>{interval}</code>\n"
        f"• <b>Base Capital:</b> <b>${capital:.2f} USDT</b>\n\n"
        f"{pos_text}"
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /status command. Show detailed trading status and positions."""
    if not await is_authorized(update, context):
        return

    text = await format_status_message(update.effective_user.id)
    await update.effective_message.reply_text(text, parse_mode="HTML")


async def format_history_message(user_id: int) -> str:
    """Format trade history and stats table for a given user."""
    if not db:
        return "❌ Database not initialized."

    trades = await db.get_trades(user_id, limit=10)
    stats = await db.get_trade_stats(user_id)

    total_trades = stats.get("total_trades", 0)
    win_rate = stats.get("win_rate", 0.0)
    total_pnl = stats.get("total_pnl", 0.0)
    wins = stats.get("wins", 0)
    losses = stats.get("losses", 0)

    pnl_sign = "+" if total_pnl >= 0 else ""
    pnl_emoji = "🟢" if total_pnl >= 0 else "🔴"

    header = (
        "📋 <b>Performance & Trade History</b>\n\n"
        f"• <b>Total Trades:</b> {total_trades} (W: {wins} / L: {losses})\n"
        f"• <b>Win Rate:</b> <b>{win_rate:.1f}%</b>\n"
        f"• <b>Total PnL:</b> {pnl_emoji} <b>{pnl_sign}${total_pnl:.2f} USDT</b>\n"
        f"• <b>Best / Worst:</b> +${stats.get('best_trade', 0):.2f} / ${stats.get('worst_trade', 0):.2f}\n\n"
    )

    if not trades:
        return header + "<i>No closed trades recorded yet. Start trading to populate history!</i>"

    lines = [header, "<b>Last 10 Executed Trades:</b>\n"]
    for t in trades:
        tid = t.get("id")
        sym = t.get("symbol", "")
        pnl = float(t.get("pnl", 0.0))
        pct = float(t.get("pnl_percent", 0.0))
        entry_p = float(t.get("entry_price", 0.0))
        exit_p = float(t.get("exit_price", 0.0))
        reason = t.get("reason", "Exit")

        sign = "+" if pnl >= 0 else ""
        emoji = "🟢" if pnl >= 0 else "🔴"

        lines.append(
            f"#{tid} <b>{sym}</b> | {emoji} <b>{sign}${pnl:.2f} ({sign}{pct:.2f}%)</b>\n"
            f"  ${entry_p:.4f} ➔ ${exit_p:.4f} | <i>{reason}</i>"
        )

    return "\n".join(lines)


async def history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /history command. Show trade log and stats."""
    if not await is_authorized(update, context):
        return

    text = await format_history_message(update.effective_user.id)
    await update.effective_message.reply_text(text, parse_mode="HTML")


async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /balance command. Real Binance balance or paper capital + open equity."""
    if not await is_authorized(update, context):
        return

    user_id = update.effective_user.id
    user = await db.get_user(user_id)
    if not user:
        await update.effective_message.reply_text("❌ User not found.")
        return

    has_keys = Config.is_live(user)
    usdt_balance = 0.0
    exchange = Exchange(
        api_key=user.get("binance_api_key", ""),
        secret_key=user.get("binance_secret_key", "")
    )

    try:
        if has_keys:
            usdt_balance = await exchange.get_usdt_balance()
            balance_source = "Binance Spot Wallet"
        else:
            usdt_balance = float(user.get("capital", 100.0))
            balance_source = "Paper Trading Balance"
    except Exception as e:
        logger.error(f"Error fetching balance for {user_id}: {e}")
        usdt_balance = float(user.get("capital", 100.0))
        balance_source = "Cached Capital"

    # Check open position equity
    pos = await db.get_open_position(user_id)
    position_equity = 0.0
    pos_desc = "None ($0.00)"

    if pos:
        sym = pos.get("symbol", "BTCUSDT")
        qty = float(pos.get("quantity", 0.0))
        try:
            curr_price = await exchange.get_price(sym)
            if curr_price:
                position_equity = qty * curr_price
                pos_desc = f"{sym} ({qty:.6f} @ ${curr_price:.4f}) = <b>${position_equity:.2f}</b>"
        except Exception:
            pos_desc = f"{sym} ({qty:.6f})"

    await exchange.close()

    total_equity = usdt_balance + position_equity

    text = (
        "💰 <b>Wallet & Capital Overview</b>\n\n"
        f"• <b>Available USDT:</b> <b>${usdt_balance:.2f} USDT</b> ({balance_source})\n"
        f"• <b>Open Position Value:</b> {pos_desc}\n"
        f"• <b>Total Net Equity:</b> <b>${total_equity:.2f} USDT</b>\n"
        f"• <b>Mode:</b> {'🚀 LIVE' if has_keys else '📝 PAPER'}"
    )

    await update.effective_message.reply_text(text, parse_mode="HTML")


async def portfolio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle /portfolio command.
    Sends text summary and WebApp button to open the Telegram Mini App.
    """
    if not await is_authorized(update, context):
        return

    user_id = update.effective_user.id
    user = await db.get_user(user_id)
    capital = user.get("capital", 100.0) if user else 100.0
    stats = await db.get_trade_stats(user_id) if db else {}
    total_pnl = stats.get("total_pnl", 0.0)
    win_rate = stats.get("win_rate", 0.0)

    webapp_url = f"{Config.WEBHOOK_URL.rstrip('/')}/webapp" if Config.WEBHOOK_URL else "https://telegram.org"

    keyboard = [
        [
            InlineKeyboardButton("📊 Open Live Portfolio", web_app=WebAppInfo(url=webapp_url))
        ],
        [
            InlineKeyboardButton("🔄 Refresh Data", callback_data="cb_portfolio")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    pnl_sign = "+" if total_pnl >= 0 else ""
    pnl_emoji = "🟢" if total_pnl >= 0 else "🔴"

    text = (
        "💼 <b>BlackSwanSI — Mini App Portfolio</b>\n\n"
        f"• <b>Current Capital:</b> ${capital:.2f} USDT\n"
        f"• <b>Total PnL:</b> {pnl_emoji} {pnl_sign}${total_pnl:.2f}\n"
        f"• <b>Win Rate:</b> {win_rate:.1f}%\n\n"
        "Tap below to launch the high-resolution interactive dashboard with live equity charts, order book telemetry, and real-time PnL."
    )

    await update.effective_message.reply_text(text, reply_markup=reply_markup, parse_mode="HTML")


async def set_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/setmode portfolio|single - choose the strategy. Takes effect next /starttrade."""
    if not await is_authorized(update, context):
        return
    uid = update.effective_user.id
    arg = (context.args[0].lower() if context.args else "")
    if arg not in ("portfolio", "single"):
        user = await db.get_user(uid) or {}
        await update.effective_message.reply_text(
            f"Current mode: <b>{user.get('strategy_mode') or 'portfolio'}</b>\n\n"
            "/setmode portfolio - trend-following across 10 coins (recommended, backtested)\n"
            "/setmode single - one pair, fast signals (<b>lost money in backtests</b>)",
            parse_mode="HTML")
        return
    if await db.get_user(uid) and (await db.get_user(uid)).get("is_trading"):
        await update.effective_message.reply_text("⚠️ Run /stoptrade first, then change mode.")
        return
    await db.update_user(uid, strategy_mode=arg)
    await update.effective_message.reply_text(f"✅ Mode set to <b>{arg}</b>. Use /starttrade to begin.", parse_mode="HTML")


async def holdings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/holdings - show every coin the portfolio engine currently holds."""
    if not await is_authorized(update, context):
        return
    uid = update.effective_user.id
    positions = await db.get_portfolio_positions(uid)
    user = await db.get_user(uid) or {}
    if not positions:
        await update.effective_message.reply_text(
            "💤 <b>No coins held</b> - the portfolio is 100% cash.\n"
            "That is normal when the market trend is down: the bot sits out downtrends on purpose.",
            parse_mode="HTML")
        return
    ex = Exchange()
    try:
        lines, total_cost, total_val = [], 0.0, 0.0
        for p in positions:
            px = await ex.get_price(p["symbol"]) or p["entry_price"]
            val = p["quantity"] * px
            pnl = val - p["entry_cost"]
            total_cost += p["entry_cost"]; total_val += val
            lines.append(f"• <b>{p['symbol'].replace('USDT','')}</b>  ${val:,.2f}  ({'+' if pnl >= 0 else ''}{pnl / p['entry_cost'] * 100:.1f}%)")
    finally:
        await ex.close()
    cash = float(user.get("capital", 0)) - total_cost
    await update.effective_message.reply_text(
        "📦 <b>Holdings</b>\n" + "\n".join(lines) +
        f"\n\nInvested: ${total_val:,.2f} | Cash: ${cash:,.2f}", parse_mode="HTML")


async def set_risk(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/setrisk conservative|balanced|growth - how much volatility the portfolio targets."""
    if not await is_authorized(update, context):
        return
    uid = update.effective_user.id
    arg = (context.args[0].lower() if context.args else "")
    if arg not in Config.RISK_PROFILES:
        cur = (await db.get_user(uid) or {}).get("risk_profile") or "balanced"
        await update.effective_message.reply_text(
            f"Current risk profile: <b>{cur}</b>\n\n"
            "/setrisk conservative - smaller swings, lower returns (backtest worst drop ~-31%)\n"
            "/setrisk balanced - default (worst drop ~-36%)\n"
            "/setrisk growth - bigger returns AND bigger drops (worst drop ~-43%)\n\n"
            "More risk means more potential profit <b>and</b> bigger losses. Past results do not guarantee future results.",
            parse_mode="HTML")
        return
    await db.update_user(uid, risk_profile=arg)
    await update.effective_message.reply_text(
        f"✅ Risk profile set to <b>{arg}</b>. It applies at the next rebalance.", parse_mode="HTML")


async def set_earn(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/setearn on|off - park the bot's idle USDT in Binance Simple Earn (Flexible) to earn interest."""
    if not await is_authorized(update, context):
        return
    uid = update.effective_user.id
    arg = (context.args[0].lower() if context.args else "")
    user = await db.get_user(uid) or {}
    if not Config.ENABLE_EARN:
        await update.effective_message.reply_text("ℹ️ Idle-cash interest is not enabled on this server.")
        return
    if arg not in ("on", "off"):
        await update.effective_message.reply_text(
            f"Idle-cash interest: <b>{'ON' if user.get('earn_enabled') else 'OFF'}</b>\n"
            f"Interest earned so far: ${float(user.get('earn_income') or 0):.2f}\n\n"
            "/setearn on - move the bot's idle USDT into Binance Simple Earn (Flexible)\n"
            "/setearn off - bring it back to Spot\n\n"
            "Only the bot's own idle cash is moved. Rates change and are not guaranteed. Funds stay on Binance and "
            "you can redeem them in the Binance app at any time. Applies to LIVE accounts only.",
            parse_mode="HTML")
        return
    await db.update_user(uid, earn_enabled=1 if arg == "on" else 0)
    await update.effective_message.reply_text(
        "✅ Idle-cash interest turned <b>" + arg.upper() + "</b>. It takes effect at the next hourly check"
        + (" (live accounts only)." if arg == "on" else "; funds will be returned to Spot."), parse_mode="HTML")
