import pytest
from fastapi.testclient import TestClient

from app.config import Config
from app.database import Database
from app.trading.exchange import Exchange
import app.main as main


class _Resp:
    def __init__(self, status, payload): self.status, self._p = status, payload
    async def json(self): return self._p
    async def text(self): return str(self._p)
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False


class _Session:
    def __init__(self, plan): self.plan, self.calls = plan, []
    def get(self, url, **kw):
        self.calls.append(url)
        for prefix, (status, payload) in self.plan.items():
            if url.startswith(prefix):
                if isinstance(status, Exception): raise status
                return _Resp(status, payload)
        raise AssertionError(url)


async def _exchange(monkeypatch, plan):
    monkeypatch.setattr(Config, "BINANCE_PUBLIC_URLS", ["https://primary.test", "https://mirror.test"])
    Exchange._preferred_url = 0
    ex = Exchange()
    sess = _Session(plan)
    async def get_session(): return sess
    monkeypatch.setattr(ex, "_get_session", get_session)
    return ex, sess


async def test_falls_back_when_primary_blocked_451(monkeypatch):
    ex, sess = await _exchange(monkeypatch, {"https://primary.test": (451, {}),
                                             "https://mirror.test": (200, {"symbol": "BTCUSDT", "price": "123.5"})})
    assert await ex.get_price("BTCUSDT") == 123.5
    assert sess.calls[0].startswith("https://primary.test") and sess.calls[1].startswith("https://mirror.test")
    await ex.get_price("BTCUSDT")                        # remembers the working endpoint
    assert sess.calls[2].startswith("https://mirror.test")


async def test_falls_back_on_network_error_and_fails_cleanly(monkeypatch):
    ex, _ = await _exchange(monkeypatch, {"https://primary.test": (OSError("down"), None),
                                          "https://mirror.test": (200, {"price": "9"})})
    assert await ex.get_price("ETHUSDT") == 9.0
    ex2, _ = await _exchange(monkeypatch, {"https://primary.test": (451, {}), "https://mirror.test": (503, {})})
    assert await ex2.get_price("ETHUSDT") is None        # never crashes, just reports no data


@pytest.fixture
async def web(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, "ADMIN_IDS", [1])
    db = Database(str(tmp_path / "h.db")); await db.init()
    monkeypatch.setattr(main, "db", db)
    yield TestClient(main.fast_app), db
    await db.close()


async def test_health_ok_and_db_failure_is_503(web, monkeypatch):
    c, db = web
    r = c.get("/health"); assert r.status_code == 200 and r.json()["status"] == "ok"
    assert r.json()["live_trading"] is False
    async def boom(*a, **k): raise RuntimeError("disk gone")
    monkeypatch.setattr(db, "get_user", boom)
    assert c.get("/health").status_code == 503


def test_production_config_requires_secrets(monkeypatch):
    monkeypatch.setattr(Config, "DEV_MODE", False)
    monkeypatch.setattr(Config, "BOT_TOKEN", "x"); monkeypatch.setattr(Config, "ADMIN_IDS", [1])
    monkeypatch.setattr(Config, "ENCRYPTION_KEY", "")
    with pytest.raises(ValueError, match="ENCRYPTION_KEY"):
        Config.validate()
    monkeypatch.setattr(Config, "ENCRYPTION_KEY", "k" * 20); monkeypatch.setattr(Config, "WEBHOOK_URL", "https://x.onrender.com")
    monkeypatch.setattr(Config, "WEBHOOK_SECRET", "")
    with pytest.raises(ValueError, match="WEBHOOK_SECRET"):
        Config.validate()


def _run_config(env_extra):
    import os, subprocess, sys
    env = {k: v for k, v in os.environ.items() if k not in ("PORT", "WEB_PORT", "WEBHOOK_URL", "RENDER_EXTERNAL_URL")}
    env.update(env_extra)
    code = ("from app.config import Config; print(Config.WEB_PORT, Config.WEBHOOK_URL); "
            "Config.validate()")
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env,
                          cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_render_environment_boots_cleanly():
    """Simulates the env a Render deploy provides: PORT + RENDER_EXTERNAL_URL + the required secrets."""
    from cryptography.fernet import Fernet
    r = _run_config({"PORT": "10000", "RENDER_EXTERNAL_URL": "https://bot.onrender.com", "DEV_MODE": "false",
                     "BOT_TOKEN": "123:abc", "ADMIN_IDS": "42", "WEBHOOK_SECRET": "s3cret-value-long-enough-123",
                     "ENCRYPTION_KEY": Fernet.generate_key().decode()})
    assert r.returncode == 0, r.stderr
    assert r.stdout.startswith("10000 https://bot.onrender.com")
    assert "Live trading: disabled" in r.stdout


def test_missing_secrets_stop_the_boot_with_a_clear_message():
    r = _run_config({"DEV_MODE": "false", "BOT_TOKEN": "123:abc", "ADMIN_IDS": "42"})
    assert r.returncode != 0 and "ENCRYPTION_KEY" in r.stderr


def test_api_docs_not_public_in_production(web):
    c, _ = web
    assert c.get("/docs").status_code == 404 and c.get("/openapi.json").status_code == 404 and c.get("/redoc").status_code == 404


def test_short_secrets_are_rejected(monkeypatch):
    monkeypatch.setattr(Config, "DEV_MODE", False)
    monkeypatch.setattr(Config, "BOT_TOKEN", "x"); monkeypatch.setattr(Config, "ADMIN_IDS", [1])
    monkeypatch.setattr(Config, "ENCRYPTION_KEY", "short")
    with pytest.raises(ValueError, match="too short"):
        Config.validate()
