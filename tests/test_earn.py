import pytest
import app.trading.portfolio_engine as pe
from app.config import Config
from app.database import Database
from app.trading.portfolio_engine import PortfolioEngine
from tests.test_portfolio_engine import FakeMulti, COINS


class FakeEarn(FakeMulti):
    def __init__(self, kinds):
        super().__init__(kinds)
        self.earn, self.fail_earn = 0.0, False
        self.redeems = 0

    async def earn_product(self):
        return None if self.fail_earn else {"productId": "P1", "latestAnnualPercentageRate": "0.04",
                                            "minPurchaseAmount": "0.1"}
    async def earn_balance(self): return None if self.fail_earn else self.earn
    async def earn_subscribe(self, pid, amt):
        if self.fail_earn: return False
        self.usdt -= amt; self.earn += amt; return True
    async def earn_redeem(self, pid, amt):
        if self.fail_earn: return False
        amt = min(amt, self.earn); self.earn -= amt; self.usdt += amt; self.redeems += 1; return True


@pytest.fixture
async def eenv(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, "PORTFOLIO_COINS", COINS)
    monkeypatch.setattr(Config, "ENABLE_EARN", True)
    db = Database(str(tmp_path / "e.db")); await db.init()
    await db.upsert_user(1, "u", "n")
    await db.update_user(1, capital=10000.0, is_trading=1, earn_enabled=1)
    pe._CLOSES_CACHE.update(t=0.0, key=None, df=None)
    eng = PortfolioEngine(1, db, None)
    eng.exchange = FakeEarn({"BTCUSDT": "up", "ETHUSDT": "up", "SOLUSDT": "down", "ADAUSDT": "down"})
    eng.exchange.usdt = 100000.0          # 90,000 of this is the user's OTHER money; the bot has 10,000
    eng.user = await db.get_user(1)
    yield eng, db
    await db.close()


async def test_sweeps_only_the_bots_own_idle_cash(eenv):
    eng, db = eenv
    await eng.run_once()
    u = await db.get_user(1)
    positions = await db.get_portfolio_positions(1)
    invested = sum(p["entry_cost"] for p in positions)
    assert 0 < u["earn_principal"] <= 10000.0 - invested          # never more than bot's idle cash
    assert eng.exchange.earn == pytest.approx(u["earn_principal"])
    assert eng.exchange.usdt >= 90000.0 - invested - 1            # the user's other $90k untouched
    buffer = max(Config.EARN_MIN_BUFFER_USDT, 10000 * Config.EARN_BUFFER_PCT / 100)
    assert 10000.0 - invested - u["earn_principal"] >= buffer - 1  # trading buffer retained


async def test_interest_booked_as_income_not_trading_pnl(eenv):
    eng, db = eenv
    await eng.run_once()
    before = await db.get_user(1)
    eng.exchange.earn += 7.50                                      # a week of interest accrues
    await eng._earn_sync_income()
    after = await db.get_user(1)
    assert after["capital"] == pytest.approx(before["capital"] + 7.5)
    assert after["earn_income"] == pytest.approx(7.5)
    assert after["earn_principal"] == pytest.approx(before["earn_principal"] + 7.5)
    assert await db.get_trades(1) == [] or all(t["pnl"] != 7.5 for t in await db.get_trades(1))


async def test_redeems_on_demand_before_buying(eenv):
    eng, db = eenv
    await db.update_user(1, earn_principal=9000.0, last_rebalance="")
    eng.exchange.earn, eng.exchange.usdt = 9000.0, 300.0          # almost no spot cash
    eng.user = await db.get_user(1)
    await eng.run_once()
    assert eng.exchange.redeems >= 1
    assert len(await db.get_portfolio_positions(1)) >= 1


async def test_opting_out_returns_everything(eenv):
    eng, db = eenv
    await db.update_user(1, earn_enabled=0, earn_principal=500.0)
    eng.exchange.earn = 500.0
    eng.user = await db.get_user(1)
    await eng.run_once()
    assert eng.exchange.earn == pytest.approx(0.0, abs=0.02)
    assert (await db.get_user(1))["earn_principal"] == pytest.approx(0.0, abs=0.02)


async def test_earn_outage_never_blocks_trading(eenv):
    eng, db = eenv
    eng.exchange.fail_earn = True
    await eng.run_once()
    assert len(await db.get_portfolio_positions(1)) >= 1           # rebalance still happened
    assert (await db.get_user(1))["earn_principal"] == 0


async def test_paper_mode_and_disabled_flag_never_use_earn(eenv, monkeypatch):
    eng, db = eenv
    eng.exchange.is_paper = True
    await eng.run_once()
    assert eng.exchange.earn == 0
    eng.exchange.is_paper = False
    monkeypatch.setattr(Config, "ENABLE_EARN", False)
    await db.update_user(1, last_rebalance="")
    eng.user = await db.get_user(1)
    await eng.run_once()
    assert eng.exchange.earn == 0
