import logging
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import ContextTypes

from app.config import Config
from app.database import Database
from app.scheduler import Scheduler
from app.handlers import admin, user

logger = logging.getLogger(__name__)

# Injected by main.py
db: Optional[Database] = None
scheduler_ref: Optional[Scheduler] = None

SUPPORTED_PAIRS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT", "DOGEUSDT"]
SUPPORTED_INTERVALS = ["1m", "5m", "15m", "30m", "1h", "4h"]


def _get_main_keyboard() -> InlineKeyboardMarkup:
    """Helper to return main menu keyboard."""
    webapp_url = f"{Config.WEBHOOK_URL.rstrip('/')}/webapp" if Config.WEBHOOK_URL else "https://telegram.org"
    portfolio_btn = (
        InlineKeyboardButton("💼 Open Live Portfolio", web_app=WebAppInfo(url=webapp_url))
        if Config.WEBHOOK_URL
        else InlineKeyboardButton("💼 Portfolio Summary", callback_data="cb_portfolio")
    )

    return InlineKeyboardMarkup([
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
    ])


async def show_settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, db_instance: Database) -> None:
    """Render the configuration settings menu with action buttons."""
    query = update.callback_query
    user_id = update.effective_user.id
    user_record = await db_instance.get_user(user_id) if db_instance else None

    pair = user_record.get("trading_pair", "BTCUSDT") if user_record else "BTCUSDT"
    interval = user_record.get("candle_interval", "15m") if user_record else "15m"
    capital = user_record.get("capital", 100.0) if user_record else 100.0
    risk = user_record.get("risk_percent", 2.0) if user_record else 2.0
    sl = user_record.get("stop_loss_percent", 2.0) if user_record else 2.0
    tp = user_record.get("take_profit_percent", 4.0) if user_record else 4.0

    has_keys = Config.is_live(user_record) if user_record else False
    mode_badge = "🚀 <b>LIVE (Binance Spot)</b>" if has_keys else "📝 <b>PAPER (Simulated)</b>"

    text = (
        "⚙️ <b>Bot Strategy & Risk Settings</b>\n\n"
        f"• <b>Execution Mode:</b> {mode_badge}\n"
        f"• <b>Trading Pair:</b> <code>{pair}</code>\n"
        f"• <b>Candle Interval:</b> <code>{interval}</code>\n"
        f"• <b>Allocated Capital:</b> <b>${capital:.2f} USDT</b>\n"
        f"• <b>Position Risk / Trade:</b> {risk}%\n"
        f"• <b>Stop Loss:</b> -{sl}% | <b>Take Profit:</b> +{tp}%\n\n"
        "<i>To change Capital, use:</i> <code>/setcapital &lt;amount&gt;</code>\n"
        "<i>To configure Binance keys, use:</i> <code>/setkeys &lt;key&gt; &lt;secret&gt;</code>\n\n"
        "Select an option below to adjust market parameters:"
    )

    keyboard = [
        [
            InlineKeyboardButton("🪙 Change Pair", callback_data="cb_change_pair"),
            InlineKeyboardButton("⏱ Change Interval", callback_data="cb_change_interval"),
        ],
        [
            InlineKeyboardButton("🔙 Back to Dashboard", callback_data="cb_main_menu"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if query:
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="HTML")
    elif update.effective_message:
        await update.effective_message.reply_text(text, reply_markup=reply_markup, parse_mode="HTML")


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Master callback query router for inline buttons."""
    query = update.callback_query
    if not query:
        return

    # Always answer callback queries first to dismiss Telegram loading spinner
    await query.answer()

    data = query.data or ""
    user_id = update.effective_user.id

    # 1. Main navigation
    if data == "cb_main_menu":
        first_name = update.effective_user.first_name or "Trader"
        welcome_text = (
            f"👋 <b>Welcome, {first_name}!</b>\n\n"
            "Welcome to <b>BlackSwanSI</b>, a rule-based crypto trading assistant.\n\n"
            "• <b>Strategy:</b> daily trend-following across 10 large coins; holds cash in downtrends\n"
            "• <b>Modes:</b> Paper (simulated money, the default) and Binance spot\n"
            "• <b>Honest note:</b> it aims to limit crashes, it does <b>not</b> guarantee profit. "
            "Losing periods happen.\n\n"
            "Tap a button below to get started or type /help for commands:"
        )
        await query.edit_message_text(
            welcome_text,
            reply_markup=_get_main_keyboard(),
            parse_mode="HTML"
        )
        return

    # 2. Status Callback
    if data == "cb_status":
        status_text = await user.format_status_message(user_id)
        keyboard = [
            [
                InlineKeyboardButton("🔄 Refresh", callback_data="cb_status"),
                InlineKeyboardButton("⚙️ Settings", callback_data="cb_settings"),
            ],
            [
                InlineKeyboardButton("🔙 Back to Dashboard", callback_data="cb_main_menu"),
            ]
        ]
        await query.edit_message_text(status_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
        return

    # 3. Settings Callback
    if data == "cb_settings":
        if db:
            await show_settings_menu(update, context, db)
        return

    # 4. Change Pair Menu
    if data == "cb_change_pair":
        buttons = []
        for i in range(0, len(SUPPORTED_PAIRS), 2):
            row = [
                InlineKeyboardButton(SUPPORTED_PAIRS[i], callback_data=f"cb_set_pair_{SUPPORTED_PAIRS[i]}")
            ]
            if i + 1 < len(SUPPORTED_PAIRS):
                row.append(
                    InlineKeyboardButton(SUPPORTED_PAIRS[i + 1], callback_data=f"cb_set_pair_{SUPPORTED_PAIRS[i + 1]}")
                )
            buttons.append(row)
        buttons.append([InlineKeyboardButton("🔙 Back to Settings", callback_data="cb_settings")])

        await query.edit_message_text(
            "🪙 <b>Select Trading Pair:</b>\n\n"
            "Choose one of the top liquid pairs on Binance:",
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode="HTML"
        )
        return

    # 5. Set Pair Selection
    if data.startswith("cb_set_pair_"):
        pair = data.replace("cb_set_pair_", "").strip().upper()
        if db:
            await db.update_user(user_id, trading_pair=pair)
        await query.answer(f"✅ Pair updated to {pair}!", show_alert=False)
        if db:
            await show_settings_menu(update, context, db)
        return

    # 6. Change Interval Menu
    if data == "cb_change_interval":
        buttons = []
        for i in range(0, len(SUPPORTED_INTERVALS), 3):
            row = [
                InlineKeyboardButton(SUPPORTED_INTERVALS[j], callback_data=f"cb_set_interval_{SUPPORTED_INTERVALS[j]}")
                for j in range(i, min(i + 3, len(SUPPORTED_INTERVALS)))
            ]
            buttons.append(row)
        buttons.append([InlineKeyboardButton("🔙 Back to Settings", callback_data="cb_settings")])

        await query.edit_message_text(
            "⏱ <b>Select Candlestick Interval:</b>\n\n"
            "Higher intervals offer stronger trend signals, while lower timeframes execute faster trades:",
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode="HTML"
        )
        return

    # 7. Set Interval Selection
    if data.startswith("cb_set_interval_"):
        interval = data.replace("cb_set_interval_", "").strip().lower()
        if db:
            await db.update_user(user_id, candle_interval=interval)
        await query.answer(f"✅ Interval updated to {interval}!", show_alert=False)
        if db:
            await show_settings_menu(update, context, db)
        return

    # 8. Start Trading Callback
    if data == "cb_start_trading":
        if scheduler_ref and scheduler_ref.is_running(user_id):
            await query.answer("Trading is already active!", show_alert=True)
            return

        if scheduler_ref:
            await scheduler_ref.start_user(user_id)

        user_rec = await db.get_user(user_id) if db else None
        pair = user_rec.get("trading_pair", "BTCUSDT") if user_rec else "BTCUSDT"
        capital = user_rec.get("capital", 100.0) if user_rec else 100.0

        keyboard = [
            [
                InlineKeyboardButton("📊 View Status", callback_data="cb_status"),
                InlineKeyboardButton("⏹️ Stop Trading", callback_data="cb_stop_trading"),
            ],
            [
                InlineKeyboardButton("🔙 Back to Dashboard", callback_data="cb_main_menu"),
            ]
        ]
        await query.edit_message_text(
            f"🟢 <b>Automated Trading Loop Started!</b>\n\n"
            f"• <b>Active Pair:</b> <code>{pair}</code>\n"
            f"• <b>Capital:</b> ${capital:.2f} USDT\n"
            "Signals are evaluated automatically twice per candlestick period.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )
        return

    # 9. Stop Trading Callback
    if data == "cb_stop_trading":
        if scheduler_ref:
            await scheduler_ref.stop_user(user_id)

        keyboard = [
            [
                InlineKeyboardButton("📈 Resume Trading", callback_data="cb_start_trading"),
                InlineKeyboardButton("🔙 Back to Dashboard", callback_data="cb_main_menu"),
            ]
        ]
        await query.edit_message_text(
            "🔴 <b>Automated Trading Stopped</b>\n\n"
            "The background execution loop is paused. Tap below to resume anytime.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )
        return

    # 10. History Callback
    if data == "cb_history":
        history_text = await user.format_history_message(user_id)
        keyboard = [
            [
                InlineKeyboardButton("🔄 Refresh", callback_data="cb_history"),
                InlineKeyboardButton("🔙 Back to Dashboard", callback_data="cb_main_menu"),
            ]
        ]
        await query.edit_message_text(
            history_text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )
        return

    # 11. Portfolio Callback
    if data == "cb_portfolio":
        user_record = await db.get_user(user_id) if db else None
        capital = user_record.get("capital", 100.0) if user_record else 100.0
        stats = await db.get_trade_stats(user_id) if db else {}
        total_pnl = stats.get("total_pnl", 0.0)
        win_rate = stats.get("win_rate", 0.0)

        webapp_url = f"{Config.WEBHOOK_URL.rstrip('/')}/webapp" if Config.WEBHOOK_URL else "https://telegram.org"

        keyboard = [
            [
                InlineKeyboardButton("📊 Open Live Portfolio", web_app=WebAppInfo(url=webapp_url))
            ],
            [
                InlineKeyboardButton("🔙 Back to Dashboard", callback_data="cb_main_menu")
            ]
        ]

        pnl_sign = "+" if total_pnl >= 0 else ""
        pnl_emoji = "🟢" if total_pnl >= 0 else "🔴"

        text = (
            "💼 <b>BlackSwanSI — Live WebApp Dashboard</b>\n\n"
            f"• <b>Base Capital:</b> ${capital:.2f} USDT\n"
            f"• <b>Total Performance:</b> {pnl_emoji} {pnl_sign}${total_pnl:.2f}\n"
            f"• <b>Win Rate:</b> {win_rate:.1f}%\n\n"
            "Launch the full mini-app to inspect candlestick telemetry, 24h portfolio charts, and order books."
        )

        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
        return

    # 12. Admin Stats Callback
    if data == "cb_admin_stats":
        if user_id not in Config.ADMIN_IDS:
            await query.answer("⛔ Admin access only.", show_alert=True)
            return

        stats_text = await admin.format_global_stats()
        keyboard = [
            [
                InlineKeyboardButton("🔄 Refresh", callback_data="cb_admin_stats"),
                InlineKeyboardButton("👥 Users", callback_data="cb_admin_users"),
            ],
            [
                InlineKeyboardButton("🔙 Back to Admin Panel", callback_data="cb_admin_panel_back")
            ]
        ]
        await query.edit_message_text(stats_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
        return

    # 13. Admin Users Callback
    if data == "cb_admin_users":
        if user_id not in Config.ADMIN_IDS:
            await query.answer("⛔ Admin access only.", show_alert=True)
            return

        users_text = await admin.format_users_list()
        keyboard = [
            [
                InlineKeyboardButton("🔄 Refresh", callback_data="cb_admin_users"),
                InlineKeyboardButton("📊 Stats", callback_data="cb_admin_stats"),
            ],
            [
                InlineKeyboardButton("🔙 Back to Admin Panel", callback_data="cb_admin_panel_back")
            ]
        ]
        # Ensure we don't exceed message text edit limits
        if len(users_text) > 4000:
            users_text = users_text[:3990] + "\n..."
        await query.edit_message_text(users_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
        return

    # 14. Admin Panel Back
    if data == "cb_admin_panel_back":
        if user_id not in Config.ADMIN_IDS:
            await query.answer("⛔ Admin access only.", show_alert=True)
            return
        await admin.admin_panel(update, context)
        return

    # 15. Admin Broadcast Help
    if data == "cb_admin_broadcast_help":
        if user_id not in Config.ADMIN_IDS:
            await query.answer("⛔ Admin access only.", show_alert=True)
            return
        help_text = (
            "📢 <b>How to Broadcast Messages:</b>\n\n"
            "Use the command:\n"
            "<code>/broadcast Your announcement text here</code>\n\n"
            "This will instantly deliver the message to all active subscribers via private message."
        )
        keyboard = [[InlineKeyboardButton("🔙 Back to Admin Panel", callback_data="cb_admin_panel_back")]]
        await query.edit_message_text(help_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
        return
