import asyncio, os, sqlite3, types
import pytest
from cryptography.fernet import Fernet
from telegram.error import BadRequest, Forbidden, NetworkError

from app.config import Config
from app import backup, keepalive, security
from app.database import Database
import app.main as main


class FakeBot:
    """Minimal stand-in for the Telegram Bot API with pinned-message behaviour."""
    def __init__(self):
        self.docs, self.pinned, self.n = {}, None, 100
        self.deleted, self.unpinned, self.get_chat_error = [], [], None

    async def send_document(self, chat_id, document, caption=None, disable_notification=True):
        self.n += 1
        self.docs[self.n] = (document.filename, document.input_file_content)
        return types.SimpleNamespace(message_id=self.n)
    async def pin_chat_message(self, chat_id, message_id, disable_notification=True): self.pinned = message_id
    async def unpin_chat_message(self, chat_id, message_id): self.unpinned.append(message_id)
    async def delete_message(self, chat_id, message_id): self.deleted.append(message_id)

    async def get_chat(self, chat_id):
        if self.get_chat_error: raise self.get_chat_error
        if not self.pinned:
            return types.SimpleNamespace(pinned_message=None)
        name, _ = self.docs[self.pinned]
        return types.SimpleNamespace(pinned_message=types.SimpleNamespace(
            document=types.SimpleNamespace(file_id=str(self.pinned), file_name=name)))
    async def get_file(self, file_id):
        data = self.docs[int(file_id)][1]
        async def dl(): return bytearray(data)
        return types.SimpleNamespace(download_as_bytearray=dl)


@pytest.fixture(autouse=True)
def cfg(monkeypatch):
    monkeypatch.setattr(Config, "ENCRYPTION_KEY", "any-long-random-string-from-render-1234567890")
    monkeypatch.setattr(Config, "ADMIN_IDS", [42])
    async def nosleep(*a, **k): return None
    monkeypatch.setattr(backup.asyncio, "sleep", nosleep)


async def _db(path):
    db = Database(path); await db.init()
    await db.upsert_user(7, "alice_secret_name", "Alice")
    return db


async def test_full_wipe_and_restore_roundtrip(tmp_path):
    bot, path = FakeBot(), str(tmp_path / "live.db")
    db = await _db(path)
    try:
        await db.update_user(7, capital=1234.0, binance_api_key="KEYKEY123456", binance_secret_key="SECSEC123456")
        mgr = backup.BackupManager(bot, db)
        assert await mgr.backup() is True
    finally:
        await db.close()
    blob = bot.docs[bot.pinned][1]
    assert b"alice_secret_name" not in blob and b"SQLite format" not in blob       # backup is opaque
    os.remove(path)                                                                  # Render wipes the disk
    assert await backup.restore_if_missing(bot, path) == "restored"
    db2 = Database(path); await db2.init()
    try:
        u = await db2.get_user(7)
        assert u["capital"] == 1234.0 and u["binance_secret_key"] == "SECSEC123456"  # data and keys survived
    finally:
        await db2.close()


async def test_backup_only_when_changed_and_old_ones_tidied(tmp_path):
    bot, db = FakeBot(), await _db(str(tmp_path / "a.db"))
    try:
        mgr = backup.BackupManager(bot, db)
        assert await mgr.backup() is True
        assert await mgr.backup() is False                      # nothing changed: no upload, no chat spam
        first = bot.pinned
        await db.update_user(7, capital=5.0)
        assert await mgr.backup() is True
        assert bot.pinned != first and first in bot.unpinned and first in bot.deleted
    finally:
        await db.close()


async def test_restore_states(tmp_path, monkeypatch):
    path = str(tmp_path / "r.db")
    bot = FakeBot()
    assert await backup.restore_if_missing(bot, path) == "none"             # first ever run
    open(path, "wb").write(b"x"); assert await backup.restore_if_missing(bot, path) == "present"; os.remove(path)
    bot.get_chat_error = Forbidden("bot can't initiate conversation")
    assert await backup.restore_if_missing(bot, path) == "none"             # admin has not opened the bot yet
    bot.get_chat_error = NetworkError("timeout")
    assert await backup.restore_if_missing(bot, path) == "error"            # inconclusive: caller must not start fresh
    bot.get_chat_error = None
    bot.n += 1; bot.docs[bot.n] = (backup.PREFIX + "-x.bin", security.cipher().encrypt(b"not a database"))
    bot.pinned = bot.n
    assert await backup.restore_if_missing(bot, path) == "error"            # corrupt backup is never installed
    assert not os.path.exists(path)


async def test_wrong_encryption_key_cannot_restore(tmp_path, monkeypatch):
    bot, db = FakeBot(), await _db(str(tmp_path / "k.db"))
    try:
        await backup.BackupManager(bot, db).backup()
    finally:
        await db.close()
    monkeypatch.setattr(Config, "ENCRYPTION_KEY", "a-completely-different-secret-key-9999")
    assert await backup.restore_if_missing(bot, str(tmp_path / "new.db")) == "error"


def test_any_long_secret_works_as_encryption_key(monkeypatch):
    monkeypatch.setattr(Config, "ENCRYPTION_KEY", "Zm9v+/ab12345678901234567890==")      # looks like Render's base64
    t = security.encrypt("hello"); assert security.decrypt(t) == "hello"
    real = Fernet.generate_key().decode()
    monkeypatch.setattr(Config, "ENCRYPTION_KEY", real)                                   # genuine Fernet keys still work
    assert Fernet(real.encode()).decrypt(security.encrypt("x")[5:].encode()) == b"x"


def test_webhook_token_is_telegram_safe_and_enforced(monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setattr(Config, "WEBHOOK_SECRET", "base64+/chars==with+slashes/AAAA")
    tok = Config.webhook_token()
    assert tok and all(c in "0123456789abcdef" for c in tok) and len(tok) == 64
    monkeypatch.setattr(main, "ptb_app", types.SimpleNamespace(bot=None))
    c = TestClient(main.fast_app)
    assert c.post("/webhook", json={}, headers={"X-Telegram-Bot-Api-Secret-Token": "nope"}).status_code == 403
    assert c.post("/webhook", json={}).status_code == 403


async def test_keepalive_is_inert_without_a_public_url(monkeypatch):
    monkeypatch.setattr(Config, "WEBHOOK_URL", "")
    await asyncio.wait_for(keepalive.run(), 1)


async def test_startup_refuses_to_run_with_empty_db_when_backup_check_is_inconclusive(monkeypatch, tmp_path):
    """If Telegram is unreachable we must NOT create a fresh DB (it could later overwrite a good backup)."""
    import telegram
    class _Ctx:
        def __init__(self, token): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
    monkeypatch.setattr(telegram, "Bot", _Ctx)
    async def inconclusive(bot, path): return "error"
    monkeypatch.setattr(backup, "restore_if_missing", inconclusive)
    monkeypatch.setattr(Config, "DEV_MODE", False); monkeypatch.setattr(Config, "BOT_TOKEN", "1:x")
    monkeypatch.setattr(Config, "BACKUP_TO_TELEGRAM", True)
    monkeypatch.setattr(Config, "DATABASE_PATH", str(tmp_path / "never.db"))
    with pytest.raises(RuntimeError, match="Refusing to start"):
        async with main.lifespan(main.fast_app):
            pass
    assert not os.path.exists(str(tmp_path / "never.db"))


async def test_startup_proceeds_when_there_is_simply_no_backup_yet(monkeypatch, tmp_path):
    """First deploy: no pinned backup exists ('none') -> normal start path is taken (we stop right after the check)."""
    import telegram
    class _Ctx:
        def __init__(self, token): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
    monkeypatch.setattr(telegram, "Bot", _Ctx)
    async def none(bot, path): return "none"
    monkeypatch.setattr(backup, "restore_if_missing", none)
    class Stop(Exception): pass
    class _DB:
        async def init(self): raise Stop()
    monkeypatch.setattr(main, "db", _DB())
    monkeypatch.setattr(Config, "DEV_MODE", False); monkeypatch.setattr(Config, "BOT_TOKEN", "1:x")
    monkeypatch.setattr(Config, "BACKUP_TO_TELEGRAM", True)
    with pytest.raises(Stop):          # reached db.init(): the 'none' state did not block startup
        async with main.lifespan(main.fast_app):
            pass
