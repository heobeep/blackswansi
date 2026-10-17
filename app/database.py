import os
import logging
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any

import aiosqlite

from app.config import Config
from app import security

_SECRET_FIELDS = ("binance_api_key", "binance_secret_key")

logger = logging.getLogger(__name__)


class Database:
    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or Config.DATABASE_PATH
        self.db: Optional[aiosqlite.Connection] = None

    async def init(self) -> None:
        """Create database directory, connect, and initialize schema."""
        db_dir = os.path.dirname(self.db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)

        self.db = await aiosqlite.connect(self.db_path)
        self.db.row_factory = aiosqlite.Row

        await self.db.execute("PRAGMA journal_mode=WAL;")

        await self.db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            is_admin INTEGER DEFAULT 0,
            is_active INTEGER DEFAULT 0,
            subscription_expiry TEXT,
            binance_api_key TEXT DEFAULT '',
            binance_secret_key TEXT DEFAULT '',
            capital REAL DEFAULT 1000.0,
            trading_pair TEXT DEFAULT 'BTCUSDT',
            candle_interval TEXT DEFAULT '1d',
            risk_percent REAL DEFAULT 1.0,
            stop_loss_percent REAL DEFAULT 8.0,
            take_profit_percent REAL DEFAULT 40.0,
            is_trading INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        );
        """)

        await self.db.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            entry_price REAL NOT NULL,
            exit_price REAL NOT NULL,
            quantity REAL NOT NULL,
            pnl REAL NOT NULL,
            pnl_percent REAL NOT NULL,
            entry_time TEXT NOT NULL,
            exit_time TEXT NOT NULL,
            reason TEXT NOT NULL
        );
        """)

        await self.db.execute("""
        CREATE TABLE IF NOT EXISTS open_positions (
            user_id INTEGER PRIMARY KEY,
            symbol TEXT NOT NULL,
            entry_price REAL NOT NULL,
            quantity REAL NOT NULL,
            stop_loss REAL NOT NULL,
            take_profit REAL NOT NULL,
            entry_time TEXT NOT NULL,
            entry_capital REAL NOT NULL
        );
        """)

        await self.db.execute("""
        CREATE TABLE IF NOT EXISTS portfolio_positions (
            user_id INTEGER NOT NULL,
            symbol TEXT NOT NULL,
            entry_price REAL NOT NULL,
            quantity REAL NOT NULL,
            entry_cost REAL NOT NULL,
            stop_price REAL NOT NULL,
            stop_order_id TEXT DEFAULT '',
            entry_time TEXT NOT NULL,
            PRIMARY KEY (user_id, symbol)
        );
        """)

        await self.db.execute("""
        CREATE TABLE IF NOT EXISTS portfolio_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            timestamp TEXT NOT NULL,
            equity REAL NOT NULL,
            price REAL NOT NULL
        );
        """)

        # Lightweight migrations for databases created by earlier versions
        async with self.db.execute("PRAGMA table_info(open_positions)") as cur:
            cols = {r[1] for r in await cur.fetchall()}
        if "stop_order_id" not in cols:
            await self.db.execute("ALTER TABLE open_positions ADD COLUMN stop_order_id TEXT DEFAULT ''")
        async with self.db.execute("PRAGMA table_info(users)") as cur:
            ucols = {r[1] for r in await cur.fetchall()}
        if "strategy_mode" not in ucols:
            await self.db.execute("ALTER TABLE users ADD COLUMN strategy_mode TEXT DEFAULT 'portfolio'")
        for col, ddl in (("earn_enabled", "INTEGER DEFAULT 0"), ("earn_principal", "REAL DEFAULT 0"),
                         ("earn_income", "REAL DEFAULT 0")):
            if col not in ucols:
                await self.db.execute(f"ALTER TABLE users ADD COLUMN {col} {ddl}")
        if "risk_profile" not in ucols:
            await self.db.execute("ALTER TABLE users ADD COLUMN risk_profile TEXT DEFAULT 'balanced'")
        if "last_rebalance" not in ucols:
            await self.db.execute("ALTER TABLE users ADD COLUMN last_rebalance TEXT DEFAULT ''")
        if "peak_price" not in cols:
            await self.db.execute("ALTER TABLE open_positions ADD COLUMN peak_price REAL DEFAULT 0")

        await self.db.commit()
        logger.info(f"Database initialized at {self.db_path}")

    async def close(self) -> None:
        """Close SQLite database connection."""
        if self.db:
            await self.db.close()
            self.db = None
            logger.info("Database connection closed")

    def _row_to_dict(self, row: Optional[aiosqlite.Row]) -> Optional[Dict[str, Any]]:
        """Convert an aiosqlite Row to a plain python dictionary."""
        if row is None:
            return None
        d = {col: row[col] for col in row.keys()}
        for f in _SECRET_FIELDS:
            if d.get(f):
                d[f] = security.decrypt(d[f])
        return d

    async def get_user(self, telegram_id: int) -> Optional[Dict[str, Any]]:
        """Fetch user by Telegram ID."""
        async with self.db.execute(
            "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return self._row_to_dict(row)

    async def upsert_user(self, telegram_id: int, username: str, first_name: str) -> None:
        """Insert user if not exists, then update username and first_name."""
        await self.db.execute(
            "INSERT OR IGNORE INTO users (telegram_id, username, first_name) VALUES (?, ?, ?)",
            (telegram_id, username or "", first_name or "")
        )
        await self.db.execute(
            "UPDATE users SET username = ?, first_name = ? WHERE telegram_id = ?",
            (username or "", first_name or "", telegram_id)
        )
        await self.db.commit()

    async def update_user(self, telegram_id: int, **fields) -> None:
        """Dynamically update specific fields for a given user."""
        if not fields:
            return
        for f in _SECRET_FIELDS:
            if f in fields and fields[f]:
                fields[f] = security.encrypt(fields[f])
        set_clause = ", ".join(f"{k} = ?" for k in fields.keys())
        values = list(fields.values()) + [telegram_id]
        await self.db.execute(
            f"UPDATE users SET {set_clause} WHERE telegram_id = ?", values
        )
        await self.db.commit()

    async def activate_user(self, telegram_id: int, days: int) -> None:
        """Activate user subscription for the given number of days."""
        expiry = (datetime.utcnow() + timedelta(days=days)).isoformat()
        await self.db.execute(
            "UPDATE users SET is_active = 1, subscription_expiry = ? WHERE telegram_id = ?",
            (expiry, telegram_id)
        )
        await self.db.commit()

    async def deactivate_user(self, telegram_id: int) -> None:
        """Deactivate a user's subscription."""
        await self.db.execute(
            "UPDATE users SET is_active = 0 WHERE telegram_id = ?",
            (telegram_id,)
        )
        await self.db.commit()

    async def get_all_users(self) -> List[Dict[str, Any]]:
        """Get all users ordered by creation date."""
        async with self.db.execute("SELECT * FROM users ORDER BY created_at DESC") as cursor:
            rows = await cursor.fetchall()
            return [self._row_to_dict(r) for r in rows]

    async def get_active_users(self) -> List[Dict[str, Any]]:
        """Get all users with active and non-expired subscriptions (or admins)."""
        all_users = await self.get_all_users()
        active_users = []
        now_dt = datetime.utcnow()
        now_iso = now_dt.isoformat()

        for user in all_users:
            if user.get("is_admin", 0) == 1:
                active_users.append(user)
                continue
            if user.get("is_active", 0) != 1:
                continue
            exp = user.get("subscription_expiry")
            if not exp:
                continue
            try:
                if datetime.fromisoformat(exp) > now_dt:
                    active_users.append(user)
            except Exception:
                if exp > now_iso:
                    active_users.append(user)

        return active_users

    async def count_non_admin_users(self) -> int:
        """Return the count of users who are not administrators."""
        async with self.db.execute("SELECT COUNT(*) FROM users WHERE is_admin = 0") as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0

    async def is_subscription_valid(self, telegram_id: int) -> bool:
        """Check if user has an active, unexpired subscription or is an admin."""
        user = await self.get_user(telegram_id)
        if not user:
            return False
        if user.get("is_admin", 0) == 1:
            return True
        if user.get("is_active", 0) != 1:
            return False

        exp = user.get("subscription_expiry")
        if not exp:
            return False

        now_dt = datetime.utcnow()
        try:
            return datetime.fromisoformat(exp) > now_dt
        except Exception:
            return exp > now_dt.isoformat()

    async def get_open_position(self, user_id: int) -> Optional[Dict[str, Any]]:
        """Get the current open position for a user, or None."""
        async with self.db.execute(
            "SELECT * FROM open_positions WHERE user_id = ?", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return self._row_to_dict(row)

    async def save_position(self, user_id: int, position: Dict[str, Any]) -> None:
        """Save or replace an open position for a user."""
        await self.db.execute(
            """INSERT OR REPLACE INTO open_positions 
               (user_id, symbol, entry_price, quantity, stop_loss, take_profit, entry_time, entry_capital,
                stop_order_id, peak_price)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                user_id,
                position.get("symbol", ""),
                float(position.get("entry_price", 0.0)),
                float(position.get("quantity", 0.0)),
                float(position.get("stop_loss", 0.0)),
                float(position.get("take_profit", 0.0)),
                position.get("entry_time", datetime.utcnow().isoformat()),
                float(position.get("entry_capital", 0.0)),
                str(position.get("stop_order_id", "") or ""),
                float(position.get("peak_price", position.get("entry_price", 0.0))),
            )
        )
        await self.db.commit()

    async def update_position(self, user_id: int, **fields) -> None:
        allowed = {"stop_loss", "take_profit", "stop_order_id", "peak_price", "quantity"}
        fields = {k: v for k, v in fields.items() if k in allowed}
        if not fields:
            return
        sets = ", ".join(f"{k} = ?" for k in fields)
        await self.db.execute(f"UPDATE open_positions SET {sets} WHERE user_id = ?",
                              list(fields.values()) + [user_id])
        await self.db.commit()

    async def clear_position(self, user_id: int) -> None:
        """Delete an open position for a user."""
        await self.db.execute("DELETE FROM open_positions WHERE user_id = ?", (user_id,))
        await self.db.commit()

    async def save_trade(self, trade: Dict[str, Any]) -> None:
        """Insert a completed trade record into history."""
        await self.db.execute(
            """INSERT INTO trades 
               (user_id, symbol, side, entry_price, exit_price, quantity, pnl, pnl_percent, entry_time, exit_time, reason)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                trade.get("user_id"),
                trade.get("symbol", ""),
                trade.get("side", "BUY"),
                float(trade.get("entry_price", 0.0)),
                float(trade.get("exit_price", 0.0)),
                float(trade.get("quantity", 0.0)),
                float(trade.get("pnl", 0.0)),
                float(trade.get("pnl_percent", 0.0)),
                trade.get("entry_time", datetime.utcnow().isoformat()),
                trade.get("exit_time", datetime.utcnow().isoformat()),
                trade.get("reason", ""),
            )
        )
        await self.db.commit()

    async def get_trades(self, user_id: int, limit: int = 20) -> List[Dict[str, Any]]:
        """Get user's trade history, ordered from newest to oldest."""
        async with self.db.execute(
            "SELECT * FROM trades WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, limit)
        ) as cursor:
            rows = await cursor.fetchall()
            return [self._row_to_dict(r) for r in rows]

    async def get_trade_stats(self, user_id: int) -> Dict[str, Any]:
        """Calculate and return trading performance statistics for a user."""
        async with self.db.execute(
            "SELECT * FROM trades WHERE user_id = ? ORDER BY id ASC", (user_id,)
        ) as cursor:
            rows = await cursor.fetchall()
            trades = [self._row_to_dict(r) for r in rows]

        total_trades = len(trades)
        if total_trades == 0:
            return {
                "total_trades": 0,
                "wins": 0,
                "losses": 0,
                "win_rate": 0.0,
                "total_pnl": 0.0,
                "avg_win": 0.0,
                "avg_loss": 0.0,
                "best_trade": 0.0,
                "worst_trade": 0.0,
            }

        wins_list = [t["pnl"] for t in trades if t["pnl"] > 0]
        losses_list = [t["pnl"] for t in trades if t["pnl"] <= 0]
        wins = len(wins_list)
        losses = len(losses_list)
        win_rate = round((wins / total_trades) * 100.0, 2)
        total_pnl = round(sum(t["pnl"] for t in trades), 4)
        avg_win = round(sum(wins_list) / wins, 4) if wins > 0 else 0.0
        avg_loss = round(sum(losses_list) / losses, 4) if losses > 0 else 0.0
        best_trade = round(max((t["pnl"] for t in trades), default=0.0), 4)
        worst_trade = round(min((t["pnl"] for t in trades), default=0.0), 4)

        return {
            "total_trades": total_trades,
            "wins": wins,
            "losses": losses,
            "win_rate": win_rate,
            "total_pnl": total_pnl,
            "avg_win": avg_win,
            "avg_loss": avg_loss,
            "best_trade": best_trade,
            "worst_trade": worst_trade,
        }

    # ── portfolio positions (one row per coin held) ─────────────
    async def get_portfolio_positions(self, user_id: int) -> List[Dict[str, Any]]:
        async with self.db.execute(
            "SELECT * FROM portfolio_positions WHERE user_id = ? ORDER BY symbol", (user_id,)
        ) as cursor:
            return [self._row_to_dict(r) for r in await cursor.fetchall()]

    async def save_portfolio_position(self, user_id: int, pos: Dict[str, Any]) -> None:
        await self.db.execute(
            """INSERT OR REPLACE INTO portfolio_positions
               (user_id, symbol, entry_price, quantity, entry_cost, stop_price, stop_order_id, entry_time)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (user_id, pos["symbol"], float(pos["entry_price"]), float(pos["quantity"]),
             float(pos["entry_cost"]), float(pos["stop_price"]), str(pos.get("stop_order_id", "") or ""),
             pos.get("entry_time", datetime.utcnow().isoformat())),
        )
        await self.db.commit()

    async def delete_portfolio_position(self, user_id: int, symbol: str) -> None:
        await self.db.execute("DELETE FROM portfolio_positions WHERE user_id = ? AND symbol = ?", (user_id, symbol))
        await self.db.commit()

    async def get_realized_pnl_since(self, user_id: int, since_iso: str) -> float:
        """Sum of realized PnL for trades closed at/after since_iso (UTC ISO string)."""
        async with self.db.execute(
            "SELECT COALESCE(SUM(pnl), 0) FROM trades WHERE user_id = ? AND exit_time >= ?",
            (user_id, since_iso),
        ) as cursor:
            row = await cursor.fetchone()
            return float(row[0] or 0.0)

    async def get_last_exit_time(self, user_id: int) -> Optional[str]:
        async with self.db.execute(
            "SELECT exit_time FROM trades WHERE user_id = ? ORDER BY id DESC LIMIT 1", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None

    async def encrypt_legacy_secrets(self) -> int:
        """One-time migration: encrypt any plaintext API keys already in the DB."""
        if not Config.ENCRYPTION_KEY:
            return 0
        n = 0
        async with self.db.execute(
            "SELECT telegram_id, binance_api_key, binance_secret_key FROM users"
        ) as cursor:
            rows = await cursor.fetchall()
        for r in rows:
            new = {}
            for f, v in (("binance_api_key", r[1]), ("binance_secret_key", r[2])):
                if v and not security.is_encrypted(v):
                    new[f] = security.encrypt(v)
            if new:
                sets = ", ".join(f"{k} = ?" for k in new)
                await self.db.execute(
                    f"UPDATE users SET {sets} WHERE telegram_id = ?", list(new.values()) + [r[0]]
                )
                n += 1
        await self.db.commit()
        return n

    async def save_snapshot(self, user_id: int, equity: float, price: float) -> None:
        """Record a portfolio equity snapshot."""
        await self.db.execute(
            "INSERT INTO portfolio_snapshots (user_id, timestamp, equity, price) VALUES (?, ?, ?, ?)",
            (user_id, datetime.utcnow().isoformat(), float(equity), float(price))
        )
        await self.db.commit()

    async def get_snapshots(self, user_id: int, hours: int = 24) -> List[Dict[str, Any]]:
        """Retrieve portfolio snapshots for the last N hours."""
        cutoff = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
        async with self.db.execute(
            "SELECT timestamp, equity, price FROM portfolio_snapshots WHERE user_id = ? AND timestamp >= ? ORDER BY timestamp ASC",
            (user_id, cutoff)
        ) as cursor:
            rows = await cursor.fetchall()
            return [{"timestamp": r["timestamp"], "equity": r["equity"], "price": r["price"]} for r in rows]
