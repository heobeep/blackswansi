import logging
from datetime import datetime
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from app.config import Config
from app.database import Database
from app.scheduler import Scheduler

logger = logging.getLogger(__name__)

# Start timestamp for tracking uptime
BOT_START_TIME = datetime.now()

# Injected by main.py
db: Optional[Database] = None
scheduler_ref: Optional[Scheduler] = None


def _is_admin(user_id: int) -> bool:
    """Check if the given user_id is in Config.ADMIN_IDS."""
    return user_id in Config.ADMIN_IDS


async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /admin command. Shows admin inline keyboard panel."""
    if not update.effective_user or update.effective_user.id not in Config.ADMIN_IDS:
        return

    keyboard = [
        [
            InlineKeyboardButton("📊 Stats", callback_data="cb_admin_stats"),
            InlineKeyboardButton("👥 Users", callback_data="cb_admin_users"),
        ],
        [
            InlineKeyboardButton("📢 Broadcast Info", callback_data="cb_admin_broadcast_help"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    text = (
        "👑 <b>Admin Control Panel</b>\n\n"
        "Select an action below or use admin commands:\n"
        "• <code>/adduser &lt;id&gt; &lt;days&gt;</code> — Grant/extend subscription\n"
        "• <code>/removeuser &lt;id&gt;</code> — Deactivate user and stop trading\n"
        "• <code>/listusers</code> — List all registered users\n"
        "• <code>/globalstats</code> — View bot uptime & active traders\n"
        "• <code>/broadcast &lt;message&gt;</code> — Send broadcast to active users\n"
        "• <code>/setlimit &lt;id&gt; &lt;capital&gt;</code> — Set max trading capital"
    )

    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup, parse_mode="HTML")
    elif update.effective_message:
        await update.effective_message.reply_text(text, reply_markup=reply_markup, parse_mode="HTML")


async def add_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle /adduser command.
    Usage: /adduser <telegram_id> <days>
    """
    if not update.effective_user or update.effective_user.id not in Config.ADMIN_IDS:
        return

    if not context.args or len(context.args) < 2:
        await update.effective_message.reply_text(
            "ℹ️ <b>Usage:</b> <code>/adduser &lt;telegram_id&gt; &lt;days&gt;</code>\n"
            "Example: <code>/adduser 123456789 30</code>",
            parse_mode="HTML"
        )
        return

    try:
        target_id = int(context.args[0])
        days = int(context.args[1])
    except ValueError:
        await update.effective_message.reply_text(
            "❌ <b>Error:</b> Both Telegram ID and days must be valid numbers.",
            parse_mode="HTML"
        )
        return

    if days <= 0:
        await update.effective_message.reply_text(
            "❌ <b>Error:</b> Days must be a positive integer.",
            parse_mode="HTML"
        )
        return

    if not db:
        await update.effective_message.reply_text("❌ Database not initialized.")
        return

    # Check maximum user threshold
    current_count = await db.count_non_admin_users()
    existing_user = await db.get_user(target_id)
    if not existing_user and current_count >= Config.MAX_USERS:
        await update.effective_message.reply_text(
            f"❌ <b>User limit reached:</b> {current_count}/{Config.MAX_USERS} users registered.\n"
            "Cannot add more non-admin users.",
            parse_mode="HTML"
        )
        return

    # Ensure user row exists and activate subscription
    await db.upsert_user(target_id, "", "")
    await db.activate_user(target_id, days)

    await update.effective_message.reply_text(
        f"✅ <b>User Activated</b>\n\n"
        f"• Telegram ID: <code>{target_id}</code>\n"
        f"• Subscription: <b>{days} days</b>\n"
        f"• Status: <b>Active</b>",
        parse_mode="HTML"
    )
    logger.info(f"Admin {update.effective_user.id} activated user {target_id} for {days} days")


async def remove_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle /removeuser command.
    Usage: /removeuser <telegram_id>
    """
    if not update.effective_user or update.effective_user.id not in Config.ADMIN_IDS:
        return

    if not context.args or len(context.args) < 1:
        await update.effective_message.reply_text(
            "ℹ️ <b>Usage:</b> <code>/removeuser &lt;telegram_id&gt;</code>\n"
            "Example: <code>/removeuser 123456789</code>",
            parse_mode="HTML"
        )
        return

    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text(
            "❌ <b>Error:</b> Telegram ID must be an integer.",
            parse_mode="HTML"
        )
        return

    if not db:
        await update.effective_message.reply_text("❌ Database not initialized.")
        return

    # Stop trading if running
    if scheduler_ref and scheduler_ref.is_running(target_id):
        await scheduler_ref.stop_user(target_id)
        logger.info(f"Stopped active trading session for deactivated user {target_id}")

    await db.deactivate_user(target_id)

    await update.effective_message.reply_text(
        f"✅ User <code>{target_id}</code> has been deactivated and trading has been halted.",
        parse_mode="HTML"
    )
    logger.info(f"Admin {update.effective_user.id} removed/deactivated user {target_id}")


async def format_users_list() -> str:
    """Format the list of all registered users."""
    if not db:
        return "❌ Database not initialized."

    users = await db.get_all_users()
    if not users:
        return "ℹ️ No users found in database."

    lines = [f"👥 <b>Registered Users ({len(users)}):</b>\n"]
    for u in users:
        uid = u["telegram_id"]
        uname = f"@{u['username']}" if u.get("username") else "No username"
        fname = u.get("first_name") or "User"
        is_trading = "🟢" if (scheduler_ref and scheduler_ref.is_running(uid)) or u.get("is_trading") == 1 else "🔴"
        is_active = "✅ Active" if u.get("is_active") == 1 else "❌ Inactive"
        exp = u.get("subscription_expiry") or "N/A"
        if exp != "N/A" and "T" in exp:
            exp = exp.split("T")[0]
        elif exp != "N/A" and len(exp) > 10:
            exp = exp[:10]

        is_adm = "👑 Admin" if (u.get("is_admin") == 1 or uid in Config.ADMIN_IDS) else "Trader"
        lines.append(
            f"• <b>{fname}</b> ({uname}) [<code>{uid}</code>] | {is_adm}\n"
            f"  Status: {is_active} | Trading: {is_trading} | Expiry: <code>{exp}</code>"
        )

    return "\n".join(lines)


async def list_users(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /listusers command. Formatted overview of all users."""
    if not update.effective_user or update.effective_user.id not in Config.ADMIN_IDS:
        return

    text = await format_users_list()

    # Split into chunks if message exceeds Telegram limit
    if len(text) > 4000:
        for chunk in [text[i:i + 4000] for i in range(0, len(text), 4000)]:
            await update.effective_message.reply_text(chunk, parse_mode="HTML")
    else:
        await update.effective_message.reply_text(text, parse_mode="HTML")


async def format_global_stats() -> str:
    """Generate global statistics text."""
    if not db:
        return "❌ Database not initialized."

    users = await db.get_all_users()
    total_users = len(users)
    active_subscribers = sum(1 for u in users if u.get("is_active") == 1)
    active_traders = scheduler_ref.count_running() if scheduler_ref else 0

    uptime = datetime.now() - BOT_START_TIME
    days = uptime.days
    hours, rem = divmod(uptime.seconds, 3600)
    minutes, seconds = divmod(rem, 60)
    uptime_str = f"{days}d {hours}h {minutes}m {seconds}s" if days > 0 else f"{hours}h {minutes}m {seconds}s"

    return (
        "📊 <b>Bot Global Statistics</b>\n\n"
        f"• <b>Total Users:</b> {total_users} / {Config.MAX_USERS}\n"
        f"• <b>Active Subscriptions:</b> {active_subscribers}\n"
        f"• <b>Active Traders (Live Loop):</b> {active_traders}\n"
        f"• <b>Uptime:</b> {uptime_str}\n"
        f"• <b>Environment:</b> {'DEV (Polling)' if Config.DEV_MODE else 'PRODUCTION (Webhook)'}\n"
        f"• <b>Database:</b> <code>{Config.DATABASE_PATH}</code>"
    )


async def global_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /globalstats command. Display system stats."""
    if not update.effective_user or update.effective_user.id not in Config.ADMIN_IDS:
        return

    text = await format_global_stats()
    await update.effective_message.reply_text(text, parse_mode="HTML")


async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle /broadcast command.
    Usage: /broadcast <message>
    Sends message to all active users.
    """
    if not update.effective_user or update.effective_user.id not in Config.ADMIN_IDS:
        return

    if not context.args:
        await update.effective_message.reply_text(
            "ℹ️ <b>Usage:</b> <code>/broadcast &lt;message&gt;</code>\n"
            "Sends an official broadcast announcement to all active subscribers.",
            parse_mode="HTML"
        )
        return

    if not db:
        await update.effective_message.reply_text("❌ Database not initialized.")
        return

    broadcast_text = " ".join(context.args)
    active_users = await db.get_active_users()

    if not active_users:
        await update.effective_message.reply_text("ℹ️ No active users found to broadcast to.")
        return

    status_msg = await update.effective_message.reply_text(
        f"⏳ Sending broadcast to {len(active_users)} active users..."
    )

    success_count = 0
    fail_count = 0

    for u in active_users:
        uid = u["telegram_id"]
        try:
            await context.bot.send_message(
                chat_id=uid,
                text=f"📢 <b>Announcement from Admin:</b>\n\n{broadcast_text}",
                parse_mode="HTML"
            )
            success_count += 1
        except Exception as e:
            logger.warning(f"Failed to send broadcast to user {uid}: {e}")
            fail_count += 1

    await status_msg.edit_text(
        f"📢 <b>Broadcast Finished</b>\n\n"
        f"• <b>Delivered:</b> {success_count}\n"
        f"• <b>Failed:</b> {fail_count}\n"
        f"• <b>Total Targets:</b> {len(active_users)}",
        parse_mode="HTML"
    )


async def set_user_limit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle /setlimit command.
    Usage: /setlimit <telegram_id> <max_capital>
    """
    if not update.effective_user or update.effective_user.id not in Config.ADMIN_IDS:
        return

    if not context.args or len(context.args) < 2:
        await update.effective_message.reply_text(
            "ℹ️ <b>Usage:</b> <code>/setlimit &lt;telegram_id&gt; &lt;max_capital&gt;</code>\n"
            "Example: <code>/setlimit 123456789 500.0</code>",
            parse_mode="HTML"
        )
        return

    try:
        target_id = int(context.args[0])
        max_capital = float(context.args[1])
    except ValueError:
        await update.effective_message.reply_text(
            "❌ <b>Error:</b> Telegram ID must be integer and capital must be a number.",
            parse_mode="HTML"
        )
        return

    if max_capital <= 0:
        await update.effective_message.reply_text("❌ Capital limit must be greater than 0.")
        return

    if not db:
        await update.effective_message.reply_text("❌ Database not initialized.")
        return

    target_user = await db.get_user(target_id)
    if not target_user:
        await update.effective_message.reply_text(f"❌ User <code>{target_id}</code> not found in database.", parse_mode="HTML")
        return

    await db.update_user(target_id, capital=max_capital)
    await update.effective_message.reply_text(
        f"✅ Capital limit for user <code>{target_id}</code> updated to <b>${max_capital:.2f} USDT</b>.",
        parse_mode="HTML"
    )
    logger.info(f"Admin {update.effective_user.id} updated user {target_id} capital limit to {max_capital}")
