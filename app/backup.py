"""
Free-tier persistence: keep an ENCRYPTED copy of the SQLite database in the admin's private Telegram chat.

Why: Render's free instances lose their disk on every restart/spin-down. The newest backup is pinned in the
admin's chat; on startup, if the database file is missing, it is downloaded, decrypted, verified and restored.

Safety rules:
  * Backups are encrypted with ENCRYPTION_KEY (Fernet). Without that key a backup is unreadable.
  * A backup is uploaded only when the database content changed.
  * Backups are enabled only if the startup restore check was conclusive. If Telegram could not be reached
    the app refuses to start rather than risk overwriting a good backup with an empty database.
  * The admin must have sent /start to the bot once (Telegram does not allow bots to message strangers).
"""
import asyncio
import hashlib
import io
import logging
import os
import sqlite3
import tempfile
from datetime import datetime
from typing import Optional

from app.config import Config
from app import security

logger = logging.getLogger(__name__)

PREFIX = "blackswansi-backup"


def _db_ok(path: str) -> bool:
    """True if `path` is a readable SQLite database that contains our users table."""
    try:
        con = sqlite3.connect(path)
        try:
            if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                return False
            return con.execute("SELECT 1 FROM sqlite_master WHERE name='users'").fetchone() is not None
        finally:
            con.close()
    except Exception:
        return False


async def restore_if_missing(bot, db_path: str, retries: int = 3) -> str:
    """
    Returns one of: "present" (DB already on disk), "restored", "none" (no backup exists: first run),
    "error" (could not tell: caller must NOT proceed to create a fresh database).
    """
    if os.path.exists(db_path) and os.path.getsize(db_path) > 0:
        return "present"
    if not Config.ADMIN_IDS or Config.ADMIN_IDS[0] == 0:
        return "none"
    from telegram.error import BadRequest, Forbidden

    admin = Config.ADMIN_IDS[0]
    for attempt in range(1, retries + 1):
        try:
            chat = await bot.get_chat(admin)
            pm = getattr(chat, "pinned_message", None)
            doc = getattr(pm, "document", None) if pm else None
            if not doc or not (doc.file_name or "").startswith(PREFIX):
                return "none"
            tg_file = await bot.get_file(doc.file_id)
            blob = bytes(await tg_file.download_as_bytearray())
            plain = security.cipher().decrypt(blob)
            os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
            fd, tmp = tempfile.mkstemp(suffix=".db", dir=os.path.dirname(os.path.abspath(db_path)))
            with os.fdopen(fd, "wb") as f:
                f.write(plain)
            if not _db_ok(tmp):
                os.remove(tmp)
                logger.error("Pinned backup failed verification; refusing to restore it")
                return "error"
            os.replace(tmp, db_path)
            logger.info("Database restored from the pinned Telegram backup")
            return "restored"
        except (BadRequest, Forbidden) as e:
            # The admin has not started the bot yet, so no backup can exist.
            logger.warning(f"No backup available (admin has not opened the bot yet?): {e}")
            return "none"
        except Exception as e:
            logger.warning(f"Backup restore attempt {attempt}/{retries} failed: {e}")
            await asyncio.sleep(3)
    return "error"


class BackupManager:
    def __init__(self, bot, db):
        self.bot, self.db = bot, db
        self.last_hash: Optional[str] = None
        self.last_message_id: Optional[int] = None
        self._warned = False

    async def _snapshot(self) -> Optional[bytes]:
        """Consistent copy of the live database (VACUUM INTO), as bytes."""
        tmp = os.path.join(tempfile.gettempdir(), f"bsi-snap-{os.getpid()}-{datetime.utcnow().timestamp()}.db")
        try:
            try:
                await self.db.db.commit()
            except Exception:
                pass
            await self.db.db.execute("VACUUM INTO ?", (tmp,))
            with open(tmp, "rb") as f:
                return f.read()
        except Exception as e:
            logger.warning(f"Could not snapshot database: {e}")
            return None
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass

    async def backup(self, force: bool = False) -> bool:
        """Upload + pin a fresh encrypted backup if the data changed. Returns True if uploaded."""
        if not Config.ENCRYPTION_KEY or not Config.ADMIN_IDS or Config.ADMIN_IDS[0] == 0:
            return False
        data = await self._snapshot()
        if not data:
            return False
        digest = hashlib.sha256(data).hexdigest()
        if digest == self.last_hash and not force:
            return False
        admin = Config.ADMIN_IDS[0]
        try:
            from telegram import InputFile
            token = security.cipher().encrypt(data)
            stamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
            msg = await self.bot.send_document(
                chat_id=admin,
                document=InputFile(io.BytesIO(token), filename=f"{PREFIX}-{stamp}.bin"),
                caption=f"🔒 BlackSwanSI encrypted backup ({stamp} UTC). Do not delete the pinned one.",
                disable_notification=True,
            )
            await self.bot.pin_chat_message(chat_id=admin, message_id=msg.message_id, disable_notification=True)
            old = self.last_message_id
            self.last_hash, self.last_message_id = digest, msg.message_id
            if old:
                for name in ("unpin_chat_message", "delete_message"):   # tidy the chat; errors are harmless
                    try:
                        await getattr(self.bot, name)(chat_id=admin, message_id=old)
                    except Exception:
                        pass
            self._warned = False
            return True
        except Exception as e:
            if not self._warned:
                logger.warning(f"Backup upload failed (will retry): {e}. "
                               f"Make sure the admin has sent /start to the bot.")
                self._warned = True
            return False

    async def run(self) -> None:
        await asyncio.sleep(20)   # let startup settle
        while True:
            try:
                await self.backup()
            except Exception as e:
                logger.warning(f"Backup loop error: {e}")
            await asyncio.sleep(max(Config.BACKUP_INTERVAL_MIN, 1) * 60)
