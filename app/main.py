import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
import urllib.parse
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
)

from app.config import Config
from app import backup, keepalive
from app.database import Database
from app.scheduler import Scheduler
from app.handlers import admin, user, callbacks

# ── Logging Configuration ────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("telegram_trading_bot")

# ── Globals ──────────────────────────────────────────────────
db = Database(Config.DATABASE_PATH)
scheduler: Optional[Scheduler] = None
ptb_app: Optional[Application] = None


async def notify_user(user_id: int, message: str) -> None:
    """Notifier callback provided to TradingEngine & Scheduler."""
    if ptb_app and ptb_app.bot:
        try:
            await ptb_app.bot.send_message(
                chat_id=user_id,
                text=message,
                parse_mode="HTML"
            )
        except Exception as e:
            logger.error(f"Failed to notify user {user_id}: {e}")


# ── Telegram initData Validation ─────────────────────────────
def validate_telegram_init_data(init_data: str) -> Optional[dict]:
    """
    Validate Telegram WebApp initData HMAC signature.
    Returns parsed user dict or None if invalid.
    """
    try:
        if not init_data:
            return None

        params = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
        received_hash = params.pop("hash", "")
        if not received_hash:
            return None

        # Sort alphabetically and format key=value strings separated by newlines
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(params.items()))

        # Secret key = HMAC-SHA256 of bot token using "WebAppData" as key
        secret_key = hmac.new(
            key=b"WebAppData",
            msg=Config.BOT_TOKEN.encode("utf-8"),
            digestmod=hashlib.sha256
        ).digest()

        # Calculated hash = HMAC-SHA256 of data_check_string using secret_key
        expected_hash = hmac.new(
            key=secret_key,
            msg=data_check_string.encode("utf-8"),
            digestmod=hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(expected_hash, received_hash):
            return None

        try:
            auth_date = int(params.get("auth_date", "0"))
        except ValueError:
            return None
        if time.time() - auth_date > 24 * 3600:
            return None  # stale / replayed initData

        user_data = json.loads(params.get("user", "{}"))
        return user_data
    except Exception as e:
        logger.warning(f"Error validating Telegram initData: {e}")
        return None


_PRICE_CACHE = {"t": 0.0, "data": {}}


async def _prices_for(symbols) -> dict:
    """One cheap public request for all prices, cached 15s and shared by every dashboard refresh."""
    if time.time() - _PRICE_CACHE["t"] > 15:
        try:
            import aiohttp
            rows = None
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as sess:
                for base in Config.BINANCE_PUBLIC_URLS:
                    try:
                        async with sess.get(f"{base}/api/v3/ticker/price") as r:
                            if r.status == 200:
                                rows = await r.json()
                                break
                    except Exception:
                        continue
            if rows:
                _PRICE_CACHE.update(t=time.time(), data={x["symbol"]: float(x["price"]) for x in rows})
        except Exception as e:
            logger.warning(f"Price fetch for dashboard failed: {e}")
    return {s: _PRICE_CACHE["data"].get(s) for s in symbols}


def _dev_bypass_allowed(request: Request) -> bool:
    """Unauthenticated browser testing: DEV_MODE only, and only from localhost."""
    if not Config.DEV_MODE:
        return False
    host = request.client.host if request.client else ""
    return host in ("127.0.0.1", "::1", "localhost")


def _inject_dependencies() -> None:
    """Inject database and scheduler singletons into handler modules."""
    user.db = db
    user.scheduler_ref = scheduler
    callbacks.db = db
    callbacks.scheduler_ref = scheduler
    admin.db = db
    admin.scheduler_ref = scheduler


def _register_handlers(app: Application) -> None:
    """Register all command and callback handlers with the PTB Application."""
    # Admin Commands
    app.add_handler(CommandHandler("admin", admin.admin_panel))
    app.add_handler(CommandHandler("adduser", admin.add_user))
    app.add_handler(CommandHandler("removeuser", admin.remove_user))
    app.add_handler(CommandHandler("listusers", admin.list_users))
    app.add_handler(CommandHandler("globalstats", admin.global_stats))
    app.add_handler(CommandHandler("broadcast", admin.broadcast))
    app.add_handler(CommandHandler("setlimit", admin.set_user_limit))

    # User Commands
    app.add_handler(CommandHandler("start", user.start))
    app.add_handler(CommandHandler("help", user.help_cmd))
    app.add_handler(CommandHandler("setkeys", user.set_keys))
    app.add_handler(CommandHandler("setcapital", user.set_capital))
    app.add_handler(CommandHandler("setpair", user.set_pair))
    app.add_handler(CommandHandler("setinterval", user.set_interval))
    app.add_handler(CommandHandler(["starttrade", "start_trade"], user.start_trading))
    app.add_handler(CommandHandler(["stoptrade", "stop_trade"], user.stop_trading))
    app.add_handler(CommandHandler("status", user.status))
    app.add_handler(CommandHandler("history", user.history))
    app.add_handler(CommandHandler("balance", user.balance))
    app.add_handler(CommandHandler("portfolio", user.portfolio))
    app.add_handler(CommandHandler("setmode", user.set_mode))
    app.add_handler(CommandHandler("setrisk", user.set_risk))
    app.add_handler(CommandHandler("setearn", user.set_earn))
    app.add_handler(CommandHandler("holdings", user.holdings))

    # Inline Keyboard Callbacks
    app.add_handler(CallbackQueryHandler(callbacks.handle_callback))
    logger.info("PTB handlers registered successfully")


# ── FastAPI Lifespan ──────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global scheduler, ptb_app
    # Startup Sequence
    Config.validate()

    # Free-tier hosting loses the disk on restart: pull the encrypted backup back BEFORE opening the database.
    restored = False
    if Config.BACKUP_TO_TELEGRAM and Config.ENCRYPTION_KEY and not Config.DEV_MODE:
        from telegram import Bot
        async with Bot(Config.BOT_TOKEN) as boot_bot:
            state = await backup.restore_if_missing(boot_bot, Config.DATABASE_PATH)
        if state == "error":
            raise RuntimeError("Could not check for a database backup (Telegram unreachable?). "
                               "Refusing to start with an empty database; the host will retry.")
        restored = state == "restored"
    await db.init()
    migrated = await db.encrypt_legacy_secrets()
    if migrated:
        logger.info(f"Encrypted API keys for {migrated} existing user(s)")
    scheduler = Scheduler(db, notify_user)
    _inject_dependencies()

    # Build PTB Application
    ptb_app = Application.builder().token(Config.BOT_TOKEN).build()
    _register_handlers(ptb_app)
    await ptb_app.initialize()

    # Webhook vs Polling
    if not Config.DEV_MODE and Config.WEBHOOK_URL:
        webhook_url = f"{Config.WEBHOOK_URL.rstrip('/')}/webhook"
        await ptb_app.bot.set_webhook(
            webhook_url,
            secret_token=Config.webhook_token() or None,
            drop_pending_updates=True,
        )
        await ptb_app.start()
        logger.info(f"Bot started in Webhook mode: {webhook_url}")
    else:
        await ptb_app.start()
        if Config.DEV_MODE:
            await ptb_app.updater.start_polling()
            logger.info("Bot started in Polling mode via FastAPI lifespan")

    # Restore active trading loops from previous sessions
    await scheduler.restore_running_users()
    logger.info("Trading engine scheduler restored active sessions")

    background = []
    backup_mgr = None
    if Config.BACKUP_TO_TELEGRAM and Config.ENCRYPTION_KEY and not Config.DEV_MODE:
        backup_mgr = backup.BackupManager(ptb_app.bot, db)
        background.append(asyncio.create_task(backup_mgr.run()))
    if Config.KEEPALIVE and Config.WEBHOOK_URL and not Config.DEV_MODE:
        background.append(asyncio.create_task(keepalive.run()))
    if restored:
        try:
            await ptb_app.bot.send_message(Config.ADMIN_IDS[0],
                                           "♻️ BlackSwanSI restarted and restored its data from the latest backup.")
        except Exception:
            pass

    yield

    # Graceful Shutdown
    logger.info("Initiating server shutdown sequence...")
    for t in background:
        t.cancel()
    if backup_mgr:
        try:
            await asyncio.wait_for(backup_mgr.backup(), timeout=20)   # last-chance backup before the disk disappears
        except Exception:
            pass
    if scheduler:
        await scheduler.stop_all()
    if ptb_app:
        if Config.DEV_MODE and ptb_app.updater and ptb_app.updater.running:
            await ptb_app.updater.stop()
        await ptb_app.stop()
        await ptb_app.shutdown()
    await db.close()
    logger.info("All services shut down cleanly")


# ── FastAPI Application ───────────────────────────────────────
fast_app = FastAPI(
    title="BlackSwanSI",
    description="Telegram crypto trading assistant with Mini App dashboard",
    lifespan=lifespan,
    # Interactive API docs are for development only; do not publish them on a live server.
    docs_url="/docs" if Config.DEV_MODE else None,
    redoc_url=None,
    openapi_url="/openapi.json" if Config.DEV_MODE else None,
)


@fast_app.get("/webapp", response_class=HTMLResponse)
async def serve_webapp():
    """Serve the single-page Telegram Mini App frontend."""
    html_path = Path(__file__).parent / "webapp" / "index.html"
    if html_path.exists():
        return HTMLResponse(content=html_path.read_text(encoding="utf-8"))
    return HTMLResponse(content="<h3>Mini App frontend index.html not found.</h3>", status_code=404)


@fast_app.get("/api/portfolio")
async def api_portfolio(request: Request):
    """
    Fetch live user portfolio state, snapshots, open position, and trade stats.
    Authenticated via Telegram initData.
    """
    init_data = request.headers.get("X-Telegram-Init-Data") or request.query_params.get("init_data", "")
    user_data = validate_telegram_init_data(init_data)

    if not user_data:
        if _dev_bypass_allowed(request):
            # Fallback for local dev browser testing
            all_users = await db.get_all_users()
            user_id = all_users[0]["telegram_id"] if all_users else (Config.ADMIN_IDS[0] if Config.ADMIN_IDS and Config.ADMIN_IDS != [0] else 1)
        else:
            return JSONResponse({"error": "Unauthorized: invalid Telegram initData"}, status_code=401)
    else:
        user_id = user_data.get("id")

    snapshots = await db.get_snapshots(user_id, hours=24 * 30)
    position = await db.get_open_position(user_id)
    stats = await db.get_trade_stats(user_id)
    user_record = await db.get_user(user_id) or {}

    mode = user_record.get("strategy_mode") or "portfolio"
    capital = float(user_record.get("capital", 0.0))          # cash + cost of open positions
    earn_income = float(user_record.get("earn_income") or 0.0)
    base_capital = capital - float(stats.get("total_pnl", 0.0) or 0.0) - earn_income   # what the user started with
    holdings, invested_value, invested_cost = [], 0.0, 0.0
    if mode == "portfolio":
        pos_rows = await db.get_portfolio_positions(user_id)
        prices = await _prices_for([p["symbol"] for p in pos_rows])
        for p in pos_rows:
            px = prices.get(p["symbol"]) or p["entry_price"]
            val = p["quantity"] * px
            invested_value += val
            invested_cost += p["entry_cost"]
            holdings.append({"symbol": p["symbol"], "quantity": p["quantity"], "entry_price": p["entry_price"],
                             "current_price": px, "value": val, "cost": p["entry_cost"],
                             "pnl": val - p["entry_cost"],
                             "pnl_pct": (val / p["entry_cost"] - 1) * 100 if p["entry_cost"] else 0.0})
        cash = capital - invested_cost
        equity = cash + invested_value
    else:
        cash, equity = capital, capital
    profile = user_record.get("risk_profile") or "balanced"
    for h in holdings:
        h["weight"] = h["value"] / equity * 100 if equity else 0.0

    return JSONResponse({
        "snapshots": snapshots,
        "position": position,
        "stats": stats,
        "capital": capital,
        "equity": equity,
        "base_capital": base_capital,
        "cash": cash,
        "holdings": holdings,
        "mode": mode,
        "risk_profile": profile,
        "earn_income": earn_income,
        "earn_principal": float(user_record.get("earn_principal") or 0.0),
        "earn_enabled": bool(user_record.get("earn_enabled")),
        "risk_info": Config.RISK_INFO.get(profile, {}),
        "is_paper": not Config.is_live(user_record),
        "last_rebalance": user_record.get("last_rebalance") or "",
        "pair": user_record.get("trading_pair", "BTCUSDT"),
        "is_trading": user_record.get("is_trading", 0),
    })


@fast_app.get("/api/trades")
async def api_trades(request: Request):
    """
    Fetch last 20 closed trade records for user.
    Authenticated via Telegram initData.
    """
    init_data = request.headers.get("X-Telegram-Init-Data") or request.query_params.get("init_data", "")
    user_data = validate_telegram_init_data(init_data)

    if not user_data:
        if _dev_bypass_allowed(request):
            all_users = await db.get_all_users()
            user_id = all_users[0]["telegram_id"] if all_users else (Config.ADMIN_IDS[0] if Config.ADMIN_IDS and Config.ADMIN_IDS != [0] else 1)
        else:
            return JSONResponse({"error": "Unauthorized: invalid Telegram initData"}, status_code=401)
    else:
        user_id = user_data.get("id")

    trades = await db.get_trades(user_id, limit=20)
    formatted = []
    for t in trades:
        formatted.append({
            "id": t["id"],
            "pair": t["symbol"],
            "symbol": t["symbol"],
            "side": t["side"],
            "entry_price": t["entry_price"],
            "exit_price": t["exit_price"],
            "quantity": t["quantity"],
            "pnl": t["pnl"],
            "pnl_percent": t["pnl_percent"],
            "entry_time": t["entry_time"],
            "exit_time": t["exit_time"],
            "reason": t["reason"],
            "timestamp": t.get("exit_time") or t.get("entry_time"),
        })

    return JSONResponse(formatted)


@fast_app.get("/health")
async def health():
    """Health check for Render/Docker/monitoring. Fails (503) if the database is unreachable."""
    try:
        await db.get_user(Config.ADMIN_IDS[0])   # cheap read proves the DB file is usable
    except Exception as e:
        logger.error(f"Health check: database unavailable: {e}")
        return JSONResponse({"status": "db_error"}, status_code=503)
    return {
        "status": "ok",
        "traders": scheduler.count_running() if scheduler else 0,
        "live_trading": Config.ALLOW_LIVE_TRADING,
        "timestamp": datetime.utcnow().isoformat()
    }


@fast_app.post("/webhook")
async def webhook(request: Request):
    """Telegram webhook incoming update endpoint."""
    if not ptb_app:
        return JSONResponse({"error": "Bot application not initialized"}, status_code=503)

    if Config.WEBHOOK_SECRET:
        got = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not hmac.compare_digest(got, Config.webhook_token()):
            return JSONResponse({"error": "forbidden"}, status_code=403)

    try:
        data = await request.json()
        update = Update.de_json(data, ptb_app.bot)
        await ptb_app.process_update(update)
        return Response("ok")
    except Exception as e:
        logger.error(f"Error processing webhook update: {e}", exc_info=True)
        return Response("error", status_code=500)


# ── Main Entrypoint ──────────────────────────────────────────
if __name__ == "__main__":
    if Config.DEV_MODE:
        async def main():
            Config.validate()
            await db.init()
            global scheduler, ptb_app
            scheduler = Scheduler(db, notify_user)
            ptb_app = Application.builder().token(Config.BOT_TOKEN).build()
            _register_handlers(ptb_app)
            _inject_dependencies()
            await scheduler.restore_running_users()
            async with ptb_app:
                await ptb_app.start()
                await ptb_app.updater.start_polling()
                print("Bot running in polling mode... Press Ctrl+C to stop")
                await asyncio.Event().wait()

        try:
            asyncio.run(main())
        except (KeyboardInterrupt, SystemExit):
            print("\nBot stopped.")
    else:
        uvicorn.run(
            "app.main:fast_app",
            host=Config.WEB_HOST,
            port=Config.WEB_PORT,
            log_level="info"
        )
