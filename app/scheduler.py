import asyncio
import logging
from typing import Callable, Dict, Optional

from app.database import Database
from app.trading.engine import TradingEngine
from app.trading.portfolio_engine import PortfolioEngine
from app.config import Config

logger = logging.getLogger(__name__)


class Scheduler:
    def __init__(self, db: Database, notifier: Callable):
        self.db = db
        self.notifier = notifier
        self._tasks: Dict[int, asyncio.Task] = {}
        self._engines: Dict[int, object] = {}

    async def start_user(self, user_id: int) -> None:
        """Start automated trading loop for user if not already running."""
        if self.is_running(user_id):
            logger.info(f"User {user_id} trading task is already active")
            return

        user = await self.db.get_user(user_id) or {}
        if (user.get("strategy_mode") or "portfolio") == "portfolio":
            engine = PortfolioEngine(user_id, self.db, self.notifier)
        else:
            engine = TradingEngine(user_id, self.db, self.notifier)
        await engine.initialize()
        self._engines[user_id] = engine

        await self.db.update_user(user_id, is_trading=1)
        task = asyncio.create_task(self._trading_loop(user_id))
        self._tasks[user_id] = task
        logger.info(f"Started trading task for user {user_id}")

    async def _trading_loop(self, user_id: int) -> None:
        """Continuous execution loop checking market conditions twice per candle interval."""
        logger.info(f"Entering trading loop for user {user_id}")
        engine = self._engines.get(user_id)

        while True:
            try:
                if not engine:
                    break

                status = await engine.run_once()
                logger.debug(f"User {user_id} cycle result: {status}")

                user = await self.db.get_user(user_id)
                if isinstance(engine, PortfolioEngine):
                    sleep_seconds = 3600.0  # daily-candle strategy: hourly check is plenty
                else:
                    interval = (user.get("candle_interval") if user else "15m") or "15m"
                    interval_secs = Config.INTERVAL_SECONDS.get(interval, 900)
                    sleep_seconds = interval_secs / 2.0

                await asyncio.sleep(sleep_seconds)
            except asyncio.CancelledError:
                logger.info(f"Trading loop for user {user_id} received cancel signal.")
                break
            except Exception as e:
                logger.error(f"Error in trading loop for user {user_id}: {e}", exc_info=True)
                try:
                    await asyncio.sleep(60)
                except asyncio.CancelledError:
                    logger.info(f"Trading loop cancelled for user {user_id} during error backoff.")
                    break

    async def stop_user(self, user_id: int) -> None:
        """Cancel user's running loop, cleanup engine, and update database state."""
        task = self._tasks.pop(user_id, None)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        engine = self._engines.pop(user_id, None)
        if engine:
            try:
                await engine.close()
            except Exception as e:
                logger.error(f"Error closing engine for user {user_id}: {e}")

        await self.db.update_user(user_id, is_trading=0)
        logger.info(f"Stopped trading for user {user_id}")

    def is_running(self, user_id: int) -> bool:
        """Check if trading task for given user is currently active."""
        task = self._tasks.get(user_id)
        return task is not None and not task.done()

    async def restore_running_users(self) -> None:
        """Resume trading loops for users marked is_trading=1 on application restart."""
        users = await self.db.get_all_users()
        for user in users:
            uid = user["telegram_id"]
            if user.get("is_trading") == 1:
                is_valid = await self.db.is_subscription_valid(uid)
                if is_valid:
                    logger.info(f"Restoring trading loop for user {uid}")
                    await self.start_user(uid)
                else:
                    logger.warning(f"Subscription expired for user {uid}; disabling trading state.")
                    await self.db.update_user(uid, is_trading=0)

    async def stop_all(self) -> None:
        """Stop all running trading tasks."""
        user_ids = list(self._tasks.keys())
        for uid in user_ids:
            await self.stop_user(uid)
        logger.info("All user trading loops stopped.")

    def count_running(self) -> int:
        """Return count of active trading tasks."""
        return sum(1 for t in self._tasks.values() if not t.done())
