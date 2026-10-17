import numpy as np, pandas as pd, pytest
from cryptography.fernet import Fernet

from app.config import Config
from app import security
from app.trading.risk import position_size, parse_fill, floor_to_step
from app.trading.strategy import StrategyParams, analyze, compute_indicators, signal_at
from app.backtest import BacktestConfig, simulate, run


def make_df(closes, spread=0.002):
    c = np.asarray(closes, float)
    o = np.r_[c[0], c[:-1]]
    idx = pd.date_range("2024-01-01", periods=len(c), freq="h")
    return pd.DataFrame({"open": o, "high": np.maximum(o, c) * (1 + spread),
                         "low": np.minimum(o, c) * (1 - spread), "close": c, "volume": 1.0}, index=idx)


def random_walk(n=3000, seed=1, drift=0.0):
    r = np.random.default_rng(seed).normal(drift, 0.004, n)
    return make_df(100 * np.exp(np.cumsum(r)))


# ---- security ------------------------------------------------------------
def test_encrypt_roundtrip_and_legacy(monkeypatch):
    monkeypatch.setattr(Config, "ENCRYPTION_KEY", Fernet.generate_key().decode())
    enc = security.encrypt("SECRET")
    assert enc != "SECRET" and security.is_encrypted(enc)
    assert security.decrypt(enc) == "SECRET"
    assert security.decrypt("plaintext-legacy") == "plaintext-legacy"
    assert security.encrypt(enc) == enc and security.encrypt("") == ""


def test_wrong_key_fails(monkeypatch):
    monkeypatch.setattr(Config, "ENCRYPTION_KEY", Fernet.generate_key().decode())
    enc = security.encrypt("SECRET")
    monkeypatch.setattr(Config, "ENCRYPTION_KEY", Fernet.generate_key().decode())
    with pytest.raises(ValueError):
        security.decrypt(enc)


@pytest.mark.asyncio
async def test_db_stores_ciphertext(monkeypatch, tmp_path):
    from app.database import Database
    monkeypatch.setattr(Config, "ENCRYPTION_KEY", Fernet.generate_key().decode())
    db = Database(str(tmp_path / "t.db")); await db.init()
    try:
        await db.upsert_user(5, "u", "n")
        await db.update_user(5, binance_api_key="PLAINKEY123", binance_secret_key="PLAINSEC456")
        async with db.db.execute("SELECT binance_api_key FROM users") as c:
            raw = (await c.fetchone())[0]
        assert raw.startswith("enc1:") and "PLAINKEY123" not in raw
        u = await db.get_user(5)
        assert u["binance_api_key"] == "PLAINKEY123" and u["binance_secret_key"] == "PLAINSEC456"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_legacy_migration(monkeypatch, tmp_path):
    from app.database import Database
    monkeypatch.setattr(Config, "ENCRYPTION_KEY", "")
    db = Database(str(tmp_path / "t.db")); await db.init()
    try:
        await db.upsert_user(5, "u", "n")
        await db.db.execute("UPDATE users SET binance_api_key='PLAINKEY123', binance_secret_key='PLAINSEC456' WHERE telegram_id=5")
        await db.db.commit()
        monkeypatch.setattr(Config, "ENCRYPTION_KEY", Fernet.generate_key().decode())
        assert await db.encrypt_legacy_secrets() == 1
        assert await db.encrypt_legacy_secrets() == 0  # idempotent
        assert (await db.get_user(5))["binance_secret_key"] == "PLAINSEC456"
    finally:
        await db.close()


# ---- risk ----------------------------------------------------------------
def test_position_size_caps():
    assert position_size(1000, 1, 2, 30) == pytest.approx(300)      # risk says 500, cap says 300
    assert position_size(1000, 0.2, 2, 30) == pytest.approx(100)    # risk-limited
    assert position_size(0, 1, 2, 30) == 0 and position_size(1000, 1, 0, 30) == 0


def test_floor_to_step():
    assert floor_to_step(0.123456, 0.001) == pytest.approx(0.123)


def test_parse_fill_base_fee_reduces_qty():
    o = {"fills": [{"qty": "1", "price": "100", "commission": "0.001", "commissionAsset": "BTC"}]}
    f = parse_fill(o, "BTC", "USDT", 0.001)
    assert f["qty"] == pytest.approx(0.999) and f["fee_quote"] == 0 and f["avg_price"] == 100


def test_parse_fill_quote_fee_and_paper():
    o = {"fills": [{"qty": "2", "price": "50", "commission": "0.1", "commissionAsset": "USDT"}]}
    assert parse_fill(o, "BTC", "USDT", 0.001)["fee_quote"] == pytest.approx(0.1)
    paper = {"side": "BUY", "executedQty": "0.9", "cummulativeQuoteQty": "99", "fee_quote": 1.0, "price": 110}
    f = parse_fill(paper, "BTC", "USDT", 0.001)
    assert f["qty"] == pytest.approx(0.9) and f["quote"] + f["fee_quote"] == pytest.approx(100)


# ---- strategy ------------------------------------------------------------
def test_insufficient_data():
    assert analyze(random_walk(30)).signal == "HOLD"


def test_trend_filter_blocks_downtrend_buys():
    df = random_walk(3000, seed=3, drift=-0.0006)
    p_on, p_off = StrategyParams(trend_ema=200), StrategyParams(trend_ema=0)
    ion, ioff = compute_indicators(df, p_on), compute_indicators(df, p_off)
    buys_on = sum(signal_at(ion, i, p_on).signal == "BUY" for i in range(250, len(df)))
    buys_off = sum(signal_at(ioff, i, p_off).signal == "BUY" for i in range(250, len(df)))
    assert buys_on < buys_off


def test_no_lookahead_in_indicators():
    df = random_walk(800)
    p = StrategyParams()
    full = compute_indicators(df, p)
    part = compute_indicators(df.iloc[:500], p)
    assert full["rsi"].iloc[499] == pytest.approx(part["rsi"].iloc[499])
    assert full["macd"].iloc[499] == pytest.approx(part["macd"].iloc[499])


# ---- backtester mechanics ---------------------------------------------------
def test_costs_make_flat_market_lose():
    """A flat price can only lose money through fees/slippage."""
    df = make_df([100.0] * 600, spread=0.0)
    cfg = BacktestConfig()
    p = StrategyParams(trend_ema=0, min_buy_signals=1, min_sell_signals=1)
    res = simulate(df, p, cfg)
    assert res.equity.iloc[-1] <= cfg.capital


def test_stop_loss_fills_at_stop_and_loses_more_than_zero():
    # force an entry then crash
    closes = list(np.linspace(100, 130, 300)) + [130 * 0.5] * 5
    df = make_df(closes)
    p = StrategyParams(trend_ema=0)
    res = simulate(df, p, BacktestConfig(sl_pct=2, tp_pct=50, trailing=False))
    losers = [t for t in res.trades if "stop" in t.reason]
    assert losers and all(t.pnl < 0 for t in losers)


def test_trailing_stop_locks_in_profit_on_crash():
    closes = list(np.linspace(100, 130, 300)) + [130 * 0.5] * 5
    res = simulate(make_df(closes), StrategyParams(trend_ema=0), BacktestConfig(sl_pct=2, tp_pct=50, trailing=True))
    stops = [t for t in res.trades if "stop" in t.reason]
    assert stops and all(t.pnl > 0 for t in stops)


def test_fills_happen_after_signal_bar():
    df = random_walk(1500, seed=5)
    res = simulate(df, StrategyParams(trend_ema=0), BacktestConfig())
    for t in res.trades:
        assert t.exit_idx >= t.entry_idx


def test_random_walk_has_no_edge_verdict():
    """On pure noise the bot must NOT be declared profitable - sanity check for the harness."""
    verdicts = [run(random_walk(6000, seed=s), BacktestConfig(), StrategyParams(), False)["verdict"]
                for s in range(6)]
    assert sum(v.startswith("PASS") for v in verdicts) <= 1


def test_oos_offset_allows_signals_at_slice_start():
    df = random_walk(2000, seed=2)
    p = StrategyParams(trend_ema=0)
    ind = compute_indicators(df, p)
    assert signal_at(ind.iloc[1000:], 5, p, offset=1000).reasons != ["Insufficient data"]
    assert signal_at(ind.iloc[1000:], 5, p, offset=0).reasons == ["Insufficient data"]
