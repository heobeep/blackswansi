import app.trading.portfolio_engine as pe
import numpy as np, pandas as pd, pytest
from datetime import datetime

from app.config import Config
from app.database import Database
from app.portfolio_backtest import PCfg, run_portfolio, live_targets, target_weights
from app.trading.portfolio_engine import PortfolioEngine

COINS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "ADAUSDT"]


def series(kind, n=400, seed=0):
    r = np.random.default_rng(seed)
    drift = {"up": 0.004, "down": -0.004, "flat": 0.0}[kind]
    return pd.Series(100 * np.exp(np.cumsum(r.normal(drift, 0.01, n))),
                     index=pd.date_range("2025-01-01", periods=n, freq="D"))


class FakeMulti:
    is_paper = False

    def __init__(self, kinds):
        self.kinds = kinds
        self.px = {s: float(series(k, seed=i).iloc[-1]) for i, (s, k) in enumerate(kinds.items())}
        self.coins = {s: 0.0 for s in kinds}
        self.usdt = 100000.0
        self.orders, self.nid = {}, 1
        self.fail_stop = False

    async def get_klines(self, s, interval, limit=500):
        i = list(self.kinds).index(s)
        return pd.DataFrame({"close": series(self.kinds[s], seed=i)})
    async def get_price(self, s): return self.px[s]
    async def get_usdt_balance(self): return self.usdt
    async def split_symbol(self, s): return s.replace("USDT", ""), "USDT"
    async def get_asset_total(self, a): return self.coins[a + "USDT"]
    async def close(self): pass

    async def place_market_buy(self, s, usdt_amount):
        q = usdt_amount / self.px[s]; self.coins[s] += q * 0.999; self.usdt -= usdt_amount
        return {"side": "BUY", "fills": [{"qty": str(q), "price": str(self.px[s]),
                "commission": str(q * 0.001), "commissionAsset": s.replace("USDT", "")}]}

    async def place_market_sell(self, s, quantity):
        self.coins[s] -= quantity; self.usdt += quantity * self.px[s]
        return {"side": "SELL", "fills": [{"qty": str(quantity), "price": str(self.px[s]),
                "commission": str(quantity * self.px[s] * 0.001), "commissionAsset": "USDT"}]}

    async def place_stop_order(self, s, q, stop):
        if self.fail_stop: return None
        oid = str(self.nid); self.nid += 1
        self.orders[oid] = {"status": "NEW", "qty": q, "stop": stop, "sym": s}; return oid
    async def get_order(self, s, oid):
        o = self.orders.get(oid)
        if not o: return None
        out = {"status": o["status"]}
        if o["status"] == "FILLED":
            out.update(executedQty=str(o["qty"]), cummulativeQuoteQty=str(o["qty"] * o["stop"]))
        return out
    async def cancel_order(self, s, oid):
        o = self.orders.get(oid)
        if o and o["status"] == "FILLED": return False
        if o: o["status"] = "CANCELED"
        return True


@pytest.fixture
async def env(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, "PORTFOLIO_COINS", COINS)
    monkeypatch.setattr(Config, "MIN_ORDER_USDT", 10.0)
    db = Database(str(tmp_path / "p.db")); await db.init()
    await db.upsert_user(1, "u", "n")
    await db.update_user(1, capital=10000.0, is_trading=1)
    pe._CLOSES_CACHE.update(t=0.0, key=None, df=None)
    msgs = []
    async def notify(uid, m): msgs.append(m)
    eng = PortfolioEngine(1, db, notify)
    eng.exchange = FakeMulti({"BTCUSDT": "up", "ETHUSDT": "up", "SOLUSDT": "down", "ADAUSDT": "down"})
    eng.user = await db.get_user(1)
    yield eng, db, msgs
    await db.close()


async def test_buys_only_uptrending_coins_with_crash_stops(env):
    eng, db, msgs = env
    await eng.run_once()
    held = {p["symbol"]: p for p in await db.get_portfolio_positions(1)}
    assert set(held) <= {"BTCUSDT", "ETHUSDT"} and held          # downtrenders never bought
    for p in held.values():
        assert p["stop_order_id"] and p["stop_price"] == pytest.approx(p["entry_price"] * 0.75)
    invested = sum(p["entry_cost"] for p in held.values())
    assert invested <= 10000.0 * 0.98                               # never exceeds budget
    assert msgs and "REBALANCE" in msgs[-1]


async def test_rebalance_runs_once_per_day(env):
    eng, db, _ = env
    await eng.run_once()
    n = len(await db.get_portfolio_positions(1))
    eng.exchange.px["BTCUSDT"] *= 1.5
    await eng.run_once()   # same day: must not trade again
    assert len(await db.get_portfolio_positions(1)) == n
    assert (await db.get_user(1))["last_rebalance"] == datetime.utcnow().date().isoformat()


async def test_trend_exit_sells_and_books_pnl(env):
    eng, db, _ = env
    await eng.run_once()
    eng.exchange.kinds["BTCUSDT"] = "down"       # BTC rolls over
    pe._CLOSES_CACHE.update(t=0.0, key=None, df=None)   # force fresh market data
    await db.update_user(1, last_rebalance="2000-01-01")
    monkey_today = datetime.utcnow().date().toordinal() % Config.PORTFOLIO_REBALANCE_DAYS
    Config.PORTFOLIO_REBALANCE_DAYS, old = 1, Config.PORTFOLIO_REBALANCE_DAYS
    try:
        eng.user = await db.get_user(1)
        await eng.run_once()
    finally:
        Config.PORTFOLIO_REBALANCE_DAYS = old
    syms = {p["symbol"] for p in await db.get_portfolio_positions(1)}
    assert "BTCUSDT" not in syms
    assert any("Trend exit" in t["reason"] for t in await db.get_trades(1))


async def test_exchange_crash_stop_is_booked(env):
    eng, db, _ = env
    await eng.run_once()
    pos = (await db.get_portfolio_positions(1))[0]
    o = eng.exchange.orders[pos["stop_order_id"]]; o["status"] = "FILLED"; eng.exchange.coins[pos["symbol"]] -= pos["quantity"]
    await eng._reconcile()
    assert pos["symbol"] not in {p["symbol"] for p in await db.get_portfolio_positions(1)}
    t = (await db.get_trades(1))[0]
    assert "crash-stop" in t["reason"] and t["pnl"] < 0


async def test_manual_sale_detected(env):
    eng, db, _ = env
    await eng.run_once()
    pos = (await db.get_portfolio_positions(1))[0]
    eng.exchange.coins[pos["symbol"]] = 0.0
    await eng._reconcile()
    assert pos["symbol"] not in {p["symbol"] for p in await db.get_portfolio_positions(1)}
    assert "outside the bot" in (await db.get_trades(1))[0]["reason"]


async def test_no_unprotected_live_positions(env):
    eng, db, _ = env
    eng.exchange.fail_stop = True
    await eng.run_once()
    assert await db.get_portfolio_positions(1) == []             # everything flattened again


async def test_partial_trim_books_proportional_pnl(env):
    eng, db, _ = env
    await eng.run_once()
    pos = (await db.get_portfolio_positions(1))[0]
    await eng._sell(pos["symbol"], eng.exchange.px[pos["symbol"]], 0.5, "Resize (risk scaling)")
    rest = next(p for p in await db.get_portfolio_positions(1) if p["symbol"] == pos["symbol"])
    assert rest["quantity"] == pytest.approx(pos["quantity"] * 0.5)
    assert rest["entry_cost"] == pytest.approx(pos["entry_cost"] * 0.5)
    assert rest["stop_order_id"] and rest["stop_order_id"] != pos["stop_order_id"]


async def test_paper_disaster_stop_triggers(env):
    eng, db, _ = env
    eng.exchange.is_paper = True
    await eng.run_once()
    pos = (await db.get_portfolio_positions(1))[0]
    eng.exchange.px[pos["symbol"]] = pos["stop_price"] * 0.99
    await eng._check_disaster_stops(await eng._prices())
    assert pos["symbol"] not in {p["symbol"] for p in await db.get_portfolio_positions(1)}


# ---- backtester / signal sanity --------------------------------------------------
def closes(kinds):
    return pd.DataFrame({s: series(k, 600, seed=i) for i, (s, k) in enumerate(kinds.items())})


def test_portfolio_no_lookahead():
    c = closes({"A": "up", "B": "down"})
    cfg = PCfg(trend_len=50, vol_target=None, rebalance_days=1, band=0)
    a = run_portfolio(c, cfg)
    c2 = c.copy(); c2.iloc[-1] *= 3          # change ONLY the last day's price
    b = run_portfolio(c2, cfg)
    assert a.iloc[:-1].equals(b.iloc[:-1])    # earlier returns unaffected by the future


def test_live_targets_respect_trend_and_budget():
    t = live_targets(closes({"A": "up", "B": "up", "C": "down", "D": "down"}), PCfg(trend_len=50, vol_target=0.3))
    assert set(t["weights"]) <= {"A", "B"} and sum(t["weights"].values()) <= 1.0 + 1e-9
    assert 0 < t["scale"] <= 1


def test_all_downtrend_means_all_cash():
    t = live_targets(closes({"A": "down", "B": "down", "C": "down"}), PCfg(trend_len=50, vol_target=0.3))
    assert t["weights"] == {}


def test_blend_weights_valid_and_trend_respecting():
    c = closes({"A": "up", "B": "up", "C": "down", "D": "down"})
    t = live_targets(c, PCfg(trend_len=50, vol_target=0.3, signal="blend"))
    assert set(t["weights"]) <= {"A", "B"} and sum(t["weights"].values()) <= 1.0 + 1e-9


def test_blend_no_lookahead():
    c = closes({"A": "up", "B": "down"})
    cfg = PCfg(trend_len=50, vol_target=0.3, rebalance_days=1, band=0, signal="blend")
    a = run_portfolio(c, cfg); c2 = c.copy(); c2.iloc[-1] *= 3
    assert a.iloc[:-1].equals(run_portfolio(c2, cfg).iloc[:-1])


async def test_risk_profile_sets_volatility_target(env):
    eng, db, _ = env
    await db.update_user(1, risk_profile="growth")
    await eng.run_once()
    assert eng.cfg.vol_target == Config.RISK_PROFILES["growth"]


async def test_market_data_is_shared_between_users(env, tmp_path):
    eng, db, _ = env
    calls = {"n": 0}
    orig = eng.exchange.get_klines
    async def counting(*a, **k):
        calls["n"] += 1
        return await orig(*a, **k)
    eng.exchange.get_klines = counting
    await eng._closes(); first = calls["n"]
    await eng._closes()
    assert calls["n"] == first   # second call served from the shared cache
