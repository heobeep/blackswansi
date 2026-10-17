import hashlib, hmac, json, time, urllib.parse
import pytest
from fastapi.testclient import TestClient

from app.config import Config
from app.database import Database
import app.main as main


def init_data(user_id, token="TESTTOKEN", age=0, tamper=False):
    user = json.dumps({"id": user_id, "first_name": "T"}, separators=(",", ":"))
    params = {"auth_date": str(int(time.time()) - age), "user": user, "query_id": "q"}
    dcs = "\n".join(f"{k}={v}" for k, v in sorted(params.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    h = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    if tamper:
        params["user"] = json.dumps({"id": 999, "first_name": "X"}, separators=(",", ":"))
    return urllib.parse.urlencode({**params, "hash": h})


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, "BOT_TOKEN", "TESTTOKEN")
    monkeypatch.setattr(Config, "DEV_MODE", False)
    db = Database(str(tmp_path / "a.db")); await db.init()
    monkeypatch.setattr(main, "db", db)
    async def fake_prices(symbols): return {s: 200.0 for s in symbols}   # BTC doubled from 100
    monkeypatch.setattr(main, "_prices_for", fake_prices)
    for uid in (1, 2):
        await db.upsert_user(uid, f"u{uid}", "n")
    await db.update_user(1, capital=1000.0, is_trading=1, risk_profile="balanced")
    await db.save_portfolio_position(1, {"symbol": "BTCUSDT", "entry_price": 100.0, "quantity": 2.0,
                                         "entry_cost": 200.0, "stop_price": 75.0})
    yield TestClient(main.fast_app), db
    await db.close()


def get(c, uid, **kw):
    return c.get("/api/portfolio", headers={"X-Telegram-Init-Data": init_data(uid, **kw)})


async def test_equity_includes_unrealised_gains(client):
    c, _ = client
    r = get(c, 1); assert r.status_code == 200
    d = r.json()
    # capital 1000 = 800 cash + 200 cost; BTC now worth 2*200=400 -> equity 1200, base 1000
    assert d["cash"] == pytest.approx(800.0) and d["equity"] == pytest.approx(1200.0)
    assert d["base_capital"] == pytest.approx(1000.0)
    assert d["holdings"][0]["pnl_pct"] == pytest.approx(100.0)
    assert d["holdings"][0]["weight"] == pytest.approx(400 / 1200 * 100)
    assert d["is_paper"] is True and d["risk_profile"] == "balanced"
    assert d["risk_info"]["worst_drop"] < 0          # honest downside is always provided


async def test_forged_and_stale_logins_rejected(client):
    c, _ = client
    assert get(c, 1, tamper=True).status_code == 401
    assert get(c, 1, age=3 * 24 * 3600).status_code == 401
    assert c.get("/api/portfolio").status_code == 401


async def test_users_cannot_see_each_others_holdings(client):
    c, _ = client
    d2 = get(c, 2).json()
    assert d2["holdings"] == [] and d2["equity"] != 1200.0


async def test_dev_bypass_is_off_in_production(client):
    c, _ = client
    assert c.get("/api/portfolio?init_data=").status_code == 401


def test_dashboard_never_shows_mock_data_to_real_users():
    html = open("app/webapp/index.html", encoding="utf-8").read()
    assert "falling back to mock" not in html
    assert "IS_DEMO" in html and "DEMO — SAMPLE DATA ONLY" in html
    # mock data may only be used inside an `if (IS_DEMO)` branch (plus the demo-only toggle button)
    assert html.count("if (IS_DEMO)") >= 2


async def test_interest_does_not_distort_returns(client):
    c, db = client
    # user started with 1000, earned 20 interest -> capital 1020; base must stay 1000, equity includes interest
    await db.update_user(1, capital=1020.0, earn_income=20.0, earn_principal=300.0)
    d = get(c, 1).json()
    assert d["base_capital"] == pytest.approx(1000.0)
    assert d["earn_income"] == pytest.approx(20.0)
