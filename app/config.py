import hashlib
import os
from dotenv import load_dotenv

load_dotenv()


def _bool(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


class Config:
    BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
    ADMIN_IDS: list = [int(x) for x in os.getenv("ADMIN_IDS", "0").split(",") if x.strip()]
    # Render sets RENDER_EXTERNAL_URL automatically; an explicit WEBHOOK_URL always wins.
    WEBHOOK_URL: str = os.getenv("WEBHOOK_URL") or os.getenv("RENDER_EXTERNAL_URL", "")
    DATABASE_PATH: str = os.getenv("DATABASE_PATH", "data/trading_bot.db")
    WEB_HOST: str = os.getenv("WEB_HOST", "0.0.0.0")
    # Render/Heroku-style hosts inject PORT; WEB_PORT is the manual override.
    WEB_PORT: int = int(os.getenv("WEB_PORT") or os.getenv("PORT") or "8000")
    MAX_USERS: int = int(os.getenv("MAX_USERS", "50"))
    DEV_MODE: bool = _bool("DEV_MODE", "false")

    # Public market-data endpoints, tried in order. If one is blocked in the server's region (HTTP 451),
    # the next is used. data-api.binance.vision serves market data only (no trading).
    BINANCE_PUBLIC_URLS: list = [u.strip().rstrip("/") for u in os.getenv(
        "BINANCE_PUBLIC_URLS", "https://api.binance.com,https://data-api.binance.vision").split(",") if u.strip()]

    # ── Security ────────────────────────────────────────────
    # Fernet key used to encrypt Binance API keys at rest.
    # Generate with:  python -m app.security genkey
    ENCRYPTION_KEY: str = os.getenv("ENCRYPTION_KEY", "")
    # Random string Telegram echoes back in a header on every webhook call.
    WEBHOOK_SECRET: str = os.getenv("WEBHOOK_SECRET", "")
    # Master switch. Even if a user stores API keys, NO real order is sent
    # unless this is true. Default false = everyone stays in paper mode.
    ALLOW_LIVE_TRADING: bool = _bool("ALLOW_LIVE_TRADING", "false")

    # ── Hosting on free/ephemeral platforms (Render free tier) ─────────────
    # Free instances sleep after 15 min idle and lose their disk on restart. These two features cope with that.
    BACKUP_TO_TELEGRAM: bool = _bool("BACKUP_TO_TELEGRAM", "true")   # encrypted DB copy in the admin's chat
    BACKUP_INTERVAL_MIN: int = int(os.getenv("BACKUP_INTERVAL_MIN", "5"))
    KEEPALIVE: bool = _bool("KEEPALIVE", "true")                      # ping our own public URL to avoid sleeping
    KEEPALIVE_MINUTES: int = int(os.getenv("KEEPALIVE_MINUTES", "10"))

    # ── Trading / risk (applies to paper AND live) ──────────
    FEE_RATE: float = float(os.getenv("FEE_RATE", "0.001"))          # 0.10% per side (Binance spot default)
    SLIPPAGE_RATE: float = float(os.getenv("SLIPPAGE_RATE", "0.0005"))  # 0.05% assumed in paper mode
    MAX_POSITION_PCT: float = float(os.getenv("MAX_POSITION_PCT", "30"))  # max % of capital in one trade
    MAX_DAILY_LOSS_PCT: float = float(os.getenv("MAX_DAILY_LOSS_PCT", "3"))  # stop opening trades after this
    COOLDOWN_CANDLES: int = int(os.getenv("COOLDOWN_CANDLES", "3"))   # wait after any exit before re-entering
    TREND_EMA: int = int(os.getenv("TREND_EMA", "200"))               # only buy above this EMA (0 = off)

    # ── Portfolio mode (default strategy) ───────────────────
    PORTFOLIO_COINS: list = [x.strip().upper() for x in os.getenv(
        "PORTFOLIO_COINS",
        "BTCUSDT,ETHUSDT,BNBUSDT,SOLUSDT,XRPUSDT,ADAUSDT,DOGEUSDT,AVAXUSDT,LINKUSDT,DOTUSDT").split(",") if x.strip()]
    PORTFOLIO_TREND_EMA: int = int(os.getenv("PORTFOLIO_TREND_EMA", "50"))
    PORTFOLIO_SIGNAL: str = os.getenv("PORTFOLIO_SIGNAL", "blend")   # blend | ema
    # Risk profiles = volatility targets (annualised). More risk = more return AND bigger drawdowns.
    RISK_PROFILES: dict = {"conservative": 0.20, "balanced": 0.30, "growth": 0.45}
    # Shown in the dashboard. Source: BACKTEST_RESULTS.md (real 2018-2026 data). Keep in sync if you re-run it.
    RISK_INFO: dict = {
        "conservative": {"worst_drop": -29, "worst_year": -27, "per_year_since_2023": 21},
        "balanced": {"worst_drop": -34, "worst_year": -32, "per_year_since_2023": 26},
        "growth": {"worst_drop": -41, "worst_year": -32, "per_year_since_2023": 29},
    }
    PORTFOLIO_VOL_TARGET: float = float(os.getenv("PORTFOLIO_VOL_TARGET", "0.30"))   # annualised
    PORTFOLIO_REBALANCE_DAYS: int = int(os.getenv("PORTFOLIO_REBALANCE_DAYS", "1"))
    DISASTER_STOP_PCT: float = float(os.getenv("DISASTER_STOP_PCT", "25"))  # exchange-side crash protection
    MIN_ORDER_USDT: float = float(os.getenv("MIN_ORDER_USDT", "10"))

    # ── Idle-cash yield via Binance Simple Earn (Flexible USDT). OFF by default. ──
    ENABLE_EARN: bool = _bool("ENABLE_EARN", "false")            # operator master switch
    EARN_BUFFER_PCT: float = float(os.getenv("EARN_BUFFER_PCT", "15"))      # % of equity kept in Spot for trading
    EARN_MIN_BUFFER_USDT: float = float(os.getenv("EARN_MIN_BUFFER_USDT", "20"))

    # Interval to seconds mapping
    INTERVAL_SECONDS: dict = {
        "1m": 60,
        "3m": 180,
        "5m": 300,
        "15m": 900,
        "30m": 1800,
        "1h": 3600,
        "2h": 7200,
        "4h": 14400,
        "1d": 86400,
    }

    @classmethod
    def webhook_token(cls) -> str:
        """Telegram only accepts [A-Za-z0-9_-] in secret_token, so derive a safe token from any secret string."""
        if not cls.WEBHOOK_SECRET:
            return ""
        return hashlib.sha256(cls.WEBHOOK_SECRET.encode()).hexdigest()

    @classmethod
    def is_live(cls, user: dict) -> bool:
        """True only if the operator enabled live trading AND the user has stored keys."""
        return bool(cls.ALLOW_LIVE_TRADING and user and user.get("binance_api_key") and user.get("binance_secret_key"))

    @classmethod
    def validate(cls):
        if not cls.BOT_TOKEN:
            raise ValueError("BOT_TOKEN is required in .env")
        if not cls.ADMIN_IDS or cls.ADMIN_IDS == [0]:
            raise ValueError("ADMIN_IDS is required in .env")
        if not cls.DEV_MODE:
            if not cls.ENCRYPTION_KEY:
                raise ValueError("ENCRYPTION_KEY is required in production (any long random string)")
            if len(cls.ENCRYPTION_KEY) < 16:
                raise ValueError("ENCRYPTION_KEY is too short (use at least 16 random characters)")
            if cls.WEBHOOK_URL and not cls.WEBHOOK_SECRET:
                raise ValueError("WEBHOOK_SECRET is required when WEBHOOK_URL is set")
            if cls.WEBHOOK_SECRET and len(cls.WEBHOOK_SECRET) < 16:
                raise ValueError("WEBHOOK_SECRET is too short (use at least 16 random characters)")
        if cls.ALLOW_LIVE_TRADING and not cls.ENCRYPTION_KEY:
            raise ValueError("ALLOW_LIVE_TRADING requires ENCRYPTION_KEY")
        print(
            f"Config OK | Admins: {cls.ADMIN_IDS} | Dev: {cls.DEV_MODE} | "
            f"Live trading: {'ENABLED' if cls.ALLOW_LIVE_TRADING else 'disabled (paper only)'}"
        )
