"""Engine behaviour against a simulated Binance: stops, reconciliation, failure handling."""
import pytest

from app.config import Config
from app.database import Database
from app.trading.engine import TradingEngine
from app.trading.risk import ratchet_stop, format_to_step


class FakeExchange:
    is_paper = False

    def __init__(self, price=100.0, usdt=1000.0):
        self.price, self.usdt = price, usdt
        self.coins = 0.0
        self.orders = {}          # id -> dict(status, qty, stop)
        self.next_id = 1
        self.fail_stop = False
        self.fail_sell = False
        self.sells = 0

    async def get_price(self, s): return self.price
    async def get_usdt_balance(self): return self.usdt
    async def split_symbol(self, s): return "BTC", "USDT"
    async def get_asset_total(self, a): return self.coins

    async def place_market_buy(self, s, usdt_amount):
        qty = usdt_amount / self.price
        self.coins += qty * 0.999
        return {"orderId": 1, "side": "BUY", "fills": [
            {"qty": str(qty), "price": str(self.price), "commission": str(qty * 0.001), "commissionAsset": "BTC"}]}

    async def place_market_sell(self, s, quantity):
        if self.fail_sell:
            return None
        self.sells += 1
        self.coins -= quantity
        return {"orderId": 2, "side": "SELL", "fills": [
            {"qty": str(quantity), "price": str(self.price), "commission": str(quantity * self.price * 0.001),
             "commissionAsset": "USDT"}]}

    async def place_stop_order(self, s, quantity, stop_price):
        if self.fail_stop:
            return None
        oid = str(self.next_id); self.next_id += 1
        self.orders[oid] = {"status": "NEW", "qty": quantity, "stop": stop_price}
        return oid

    async def get_order(self, s, oid):
        o = self.orders.get(oid)
        if not o: return None
        out = {"status": o["status"]}
        if o["status"] == "FILLED":
            out.update(executedQty=str(o["qty"]), cummulativeQuoteQty=str(o["qty"] * o["stop"] * 0.999))
        return out

    async def cancel_order(self, s, oid):
        o = self.orders.get(oid)
        if o and o["status"] == "FILLED":
            return False
        if o: o["status"] = "CANCELED"
        return True

    # test helpers
    def fire_stop(self, oid):
        self.orders[oid]["status"] = "FILLED"
        self.coins -= self.orders[oid]["qty"]


@pytest.fixture
async def env(tmp_path):
    db = Database(str(tmp_path / "t.db")); await db.init()
    await db.upsert_user(1, "u", "n")
    await db.update_user(1, capital=1000.0, trading_pair="BTCUSDT", is_trading=1,
                         risk_percent=1.0, stop_loss_percent=2.0, take_profit_percent=4.0)
    msgs = []
    async def notify(uid, m): msgs.append(m)
    eng = TradingEngine(1, db, notify)
    eng.exchange = FakeExchange()
    eng.user = await db.get_user(1)
    yield eng, db, msgs
    await db.close()


async def test_buy_places_exchange_stop(env):
    eng, db, _ = env
    await eng._open_long(100.0, ["test"])
    pos = await db.get_open_position(1)
    assert pos and pos["stop_order_id"] == "1"
    assert eng.exchange.orders["1"]["stop"] == pytest.approx(pos["stop_loss"])
    assert pos["quantity"] == pytest.approx(eng.exchange.coins, rel=1e-6)  # fee netted from qty


async def test_unprotected_position_is_flattened(env):
    eng, db, msgs = env
    eng.exchange.fail_stop = True
    await eng._open_long(100.0, ["test"])
    assert await db.get_open_position(1) is None      # closed again immediately
    assert eng.exchange.sells == 1
    assert len(await db.get_trades(1)) == 1


async def test_exchange_stop_fill_is_booked(env):
    eng, db, msgs = env
    await eng._open_long(100.0, ["test"])
    pos = await db.get_open_position(1)
    eng.exchange.price = 97.0
    eng.exchange.fire_stop(pos["stop_order_id"])
    await eng._reconcile_live()
    assert await db.get_open_position(1) is None
    trades = await db.get_trades(1)
    assert len(trades) == 1 and trades[0]["pnl"] < 0 and "stop" in trades[0]["reason"].lower()
    assert (await db.get_user(1))["capital"] < 1000.0


async def test_manual_sale_detected(env):
    eng, db, _ = env
    await eng._open_long(100.0, ["test"])
    eng.exchange.coins = 0.0                      # user sold everything in the Binance app
    await eng._reconcile_live()
    assert await db.get_open_position(1) is None
    assert "outside the bot" in (await db.get_trades(1))[0]["reason"]


async def test_cancelled_stop_is_rearmed(env):
    eng, db, _ = env
    await eng._open_long(100.0, ["test"])
    pos = await db.get_open_position(1)
    eng.exchange.orders[pos["stop_order_id"]]["status"] = "CANCELED"
    await eng._reconcile_live()
    pos2 = await db.get_open_position(1)
    assert pos2 and pos2["stop_order_id"] != pos["stop_order_id"]


async def test_cancelled_stop_that_cannot_rearm_flattens(env):
    eng, db, _ = env
    await eng._open_long(100.0, ["test"])
    pos = await db.get_open_position(1)
    eng.exchange.orders[pos["stop_order_id"]]["status"] = "CANCELED"
    eng.exchange.fail_stop = True
    await eng._reconcile_live()
    assert await db.get_open_position(1) is None


async def test_bot_sell_cancels_stop_first(env):
    eng, db, _ = env
    await eng._open_long(100.0, ["test"])
    sid = (await db.get_open_position(1))["stop_order_id"]
    await eng._close_position(103.0, "Strategy SELL signal")
    assert eng.exchange.orders[sid]["status"] == "CANCELED" and eng.exchange.sells == 1
    assert await db.get_open_position(1) is None


async def test_failed_sell_restores_protection(env):
    eng, db, _ = env
    await eng._open_long(100.0, ["test"])
    eng.exchange.fail_sell = True
    await eng._close_position(103.0, "Strategy SELL signal")
    pos = await db.get_open_position(1)
    assert pos and pos["stop_order_id"] not in ("", "1")   # a fresh stop is guarding the position


async def test_trailing_moves_stop_up_on_exchange(env):
    eng, db, _ = env
    await eng._open_long(100.0, ["test"])
    old = await db.get_open_position(1)
    await eng._update_trailing(103.0)           # +3% -> past 1R
    new = await db.get_open_position(1)
    assert new["stop_loss"] > old["stop_loss"] * 1.02          # moved to ~break-even or better
    assert new["stop_order_id"] != old["stop_order_id"]
    assert eng.exchange.orders[old["stop_order_id"]]["status"] == "CANCELED"
    await eng._update_trailing(101.0)           # price falls back: stop must NOT loosen
    assert (await db.get_open_position(1))["stop_loss"] == pytest.approx(new["stop_loss"])


async def test_live_entry_capped_by_real_balance(env):
    eng, db, _ = env
    eng.exchange.usdt = 50.0                    # only $50 actually on Binance
    await eng._open_long(100.0, ["test"])
    pos = await db.get_open_position(1)
    assert pos["entry_capital"] <= 50.0 * 1.001


def test_ratchet_never_loosens_and_locks_breakeven():
    assert ratchet_stop(100, 100.5, 98, 2, 0.001) == 98                    # not yet 1R
    be = ratchet_stop(100, 102, 98, 2, 0.001)
    assert be >= 100.2                                                     # break-even + fees
    assert ratchet_stop(100, 110, 98, 2, 0.001) == pytest.approx(107.8)    # trails 2% below peak
    assert ratchet_stop(100, 101, 107.8, 2, 0.001) == 107.8                # never down


def test_format_to_step():
    assert format_to_step(0.123456, 0.001) == "0.123"
    assert format_to_step(64210.789, 0.01) == "64210.78"
    assert format_to_step(5.0, 1.0) == "5"
