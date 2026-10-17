import asyncio
import logging
import time
from typing import Optional, Dict, Any
from concurrent.futures import ThreadPoolExecutor

import aiohttp
import pandas as pd

from app.config import Config
from app.trading.risk import floor_to_step, format_to_step

logger = logging.getLogger(__name__)


class Exchange:
    _preferred_url = 0   # index of the last public endpoint that worked (shared by all instances)

    def __init__(self, api_key: str = "", secret_key: str = ""):
        self.api_key = api_key.strip() if api_key else ""
        self.secret_key = secret_key.strip() if secret_key else ""
        self.has_keys = bool(self.api_key) and bool(self.secret_key)
        # Real orders only if the user has keys AND the operator enabled live trading.
        self.is_paper = not (self.has_keys and Config.ALLOW_LIVE_TRADING)
        self._session: Optional[aiohttp.ClientSession] = None
        self._executor = ThreadPoolExecutor(max_workers=2)
        self._client: Optional[Any] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        """Return or lazily create an aiohttp ClientSession."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    def _get_client(self, for_orders: bool = True) -> Optional[Any]:
        """Return or lazily create python-binance Client."""
        if for_orders and self.is_paper:
            return None
        if not self.has_keys:
            return None
        if self._client is None:
            try:
                from binance.client import Client
                self._client = Client(self.api_key, self.secret_key)
            except Exception as e:
                logger.error(f"Failed to instantiate Binance Client: {e}")
                return None
        return self._client

    async def _public_get(self, path: str, params: Dict[str, Any]) -> Optional[Any]:
        """GET a public Binance endpoint, falling back across Config.BINANCE_PUBLIC_URLS on any failure
        (e.g. HTTP 451 when the server runs in a region Binance blocks)."""
        urls = Config.BINANCE_PUBLIC_URLS or ["https://api.binance.com"]
        session = await self._get_session()
        start = Exchange._preferred_url % len(urls)
        last = ""
        for k in range(len(urls)):
            i = (start + k) % len(urls)
            try:
                async with session.get(f"{urls[i]}{path}", params=params,
                                       timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        Exchange._preferred_url = i
                        return await resp.json()
                    last = f"{urls[i]} -> HTTP {resp.status}"
            except Exception as e:
                last = f"{urls[i]} -> {e}"
        logger.error(f"Public request {path} failed on all endpoints (last: {last})")
        return None

    async def get_price(self, symbol: str) -> Optional[float]:
        """Fetch current ticker price via Binance public REST API."""
        data = await self._public_get("/api/v3/ticker/price", {"symbol": symbol.upper()})
        try:
            return float(data["price"]) if data else None
        except (KeyError, TypeError, ValueError):
            return None

    async def get_klines(self, symbol: str, interval: str, limit: int = 200) -> Optional[pd.DataFrame]:
        """
        Fetch CLOSED OHLCV candles via Binance public REST API.
        Returns DataFrame indexed by open_time with columns open, high, low, close, volume.
        """
        raw_data = await self._public_get("/api/v3/klines",
                                          {"symbol": symbol.upper(), "interval": interval, "limit": limit})
        if not raw_data:
            return None
        try:
            df = pd.DataFrame(raw_data, columns=[
                "open_time", "open", "high", "low", "close", "volume", "close_time", "quote_asset_volume",
                "number_of_trades", "taker_buy_base_asset_volume", "taker_buy_quote_asset_volume", "ignore"])
            # Drop the still-forming candle: only closed candles are safe to analyse.
            now_ms = int(time.time() * 1000)
            df = df[df["close_time"].astype("int64") < now_ms]
            df = df[["open_time", "open", "high", "low", "close", "volume"]].copy()
            df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
            for col in ["open", "high", "low", "close", "volume"]:
                df[col] = df[col].astype(float)
            df.set_index("open_time", inplace=True)
            return df
        except Exception as e:
            logger.error(f"Could not parse klines for {symbol}: {e}")
            return None

    async def get_usdt_balance(self) -> float:
        """Get available USDT balance. Returns simulated 100.0 if in paper trading mode."""
        if self.is_paper:
            return 100.0

        try:
            client = self._get_client()
            if not client:
                return 0.0
            loop = asyncio.get_running_loop()
            res = await loop.run_in_executor(
                self._executor, lambda: client.get_asset_balance(asset="USDT")
            )
            if res and "free" in res:
                return float(res["free"])
            return 0.0
        except Exception as e:
            logger.error(f"Error fetching USDT balance: {e}")
            return 0.0

    async def get_asset_balance(self, asset: str) -> float:
        """Get available balance for a specific coin asset."""
        if self.is_paper:
            return 0.0

        try:
            client = self._get_client()
            if not client:
                return 0.0
            loop = asyncio.get_running_loop()
            res = await loop.run_in_executor(
                self._executor, lambda: client.get_asset_balance(asset=asset.upper())
            )
            if res and "free" in res:
                return float(res["free"])
            return 0.0
        except Exception as e:
            logger.error(f"Error fetching {asset} balance: {e}")
            return 0.0

    async def place_market_buy(self, symbol: str, usdt_amount: float) -> Optional[Dict[str, Any]]:
        """Place a market buy order using quoteOrderQty (USDT amount)."""
        symbol = symbol.upper()
        if self.is_paper:
            estimated_price = await self.get_price(symbol)
            if not estimated_price or estimated_price <= 0:
                estimated_price = 1.0
            fill_price = estimated_price * (1.0 + Config.SLIPPAGE_RATE)
            fee = usdt_amount * Config.FEE_RATE
            qty = (usdt_amount - fee) / fill_price
            return {
                "orderId": f"PAPER_{int(time.time())}",
                "symbol": symbol,
                "side": "BUY",
                "price": fill_price,
                "executedQty": str(qty),
                "cummulativeQuoteQty": str(usdt_amount - fee),
                "fee_quote": fee,
                "status": "FILLED",
                "paper": True,
            }

        try:
            client = self._get_client()
            if not client:
                return None
            loop = asyncio.get_running_loop()
            order = await loop.run_in_executor(
                self._executor,
                lambda: client.order_market_buy(
                    symbol=symbol, quoteOrderQty=round(usdt_amount, 2)
                ),
            )
            return order
        except Exception as e:
            logger.error(f"Error placing market buy for {symbol}: {e}")
            return None

    async def place_market_sell(self, symbol: str, quantity: float) -> Optional[Dict[str, Any]]:
        """Place a market sell order for the specified asset quantity."""
        symbol = symbol.upper()
        if self.is_paper:
            estimated_price = await self.get_price(symbol)
            if not estimated_price or estimated_price <= 0:
                estimated_price = 1.0
            fill_price = estimated_price * (1.0 - Config.SLIPPAGE_RATE)
            gross = quantity * fill_price
            return {
                "orderId": f"PAPER_{int(time.time())}",
                "symbol": symbol,
                "side": "SELL",
                "price": fill_price,
                "executedQty": str(quantity),
                "cummulativeQuoteQty": str(gross),
                "fee_quote": gross * Config.FEE_RATE,
                "status": "FILLED",
                "paper": True,
            }

        try:
            client = self._get_client()
            if not client:
                return None
            loop = asyncio.get_running_loop()
            step, _tick = await self._symbol_steps(client, symbol)
            qty_str = format_to_step(quantity, step)
            if float(qty_str) <= 0:
                logger.error(f"Sell quantity rounds to zero for {symbol} (qty={quantity})")
                return None
            order = await loop.run_in_executor(
                self._executor,
                lambda: client.order_market_sell(symbol=symbol, quantity=qty_str),
            )
            return order
        except Exception as e:
            logger.error(f"Error placing market sell for {symbol}: {e}")
            return None

    async def _symbol_steps(self, client: Any, symbol: str) -> tuple:
        """(LOT_SIZE stepSize, PRICE_FILTER tickSize); 0 for unknown."""
        step = tick = 0.0
        try:
            loop = asyncio.get_running_loop()
            info = await loop.run_in_executor(self._executor, lambda: client.get_symbol_info(symbol))
            for f in (info or {}).get("filters", []):
                if f.get("filterType") == "LOT_SIZE":
                    step = float(f["stepSize"])
                elif f.get("filterType") == "PRICE_FILTER":
                    tick = float(f["tickSize"])
        except Exception as e:
            logger.warning(f"Could not read symbol filters for {symbol}: {e}")
        return step, tick

    async def place_stop_order(self, symbol: str, quantity: float, stop_price: float) -> Optional[str]:
        """
        Exchange-side protective stop (STOP_LOSS_LIMIT sell). Lives on Binance, so it fires even if
        this bot is down. The limit sits 0.5% under the trigger so it fills in a fast drop.
        Returns the order id, or None on failure.
        """
        symbol = symbol.upper()
        if self.is_paper:
            return "PAPER_STOP"
        try:
            client = self._get_client()
            if not client:
                return None
            loop = asyncio.get_running_loop()
            step, tick = await self._symbol_steps(client, symbol)
            qty_str = format_to_step(quantity, step)
            stop_str = format_to_step(stop_price, tick)
            limit_str = format_to_step(stop_price * 0.995, tick)
            order = await loop.run_in_executor(
                self._executor,
                lambda: client.create_order(
                    symbol=symbol, side="SELL", type="STOP_LOSS_LIMIT", timeInForce="GTC",
                    quantity=qty_str, stopPrice=stop_str, price=limit_str,
                ),
            )
            return str(order["orderId"])
        except Exception as e:
            logger.error(f"Error placing stop order for {symbol}: {e}")
            return None

    async def get_order(self, symbol: str, order_id: str) -> Optional[Dict[str, Any]]:
        if self.is_paper or not order_id or str(order_id).startswith("PAPER"):
            return None
        try:
            client = self._get_client()
            if not client:
                return None
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                self._executor, lambda: client.get_order(symbol=symbol.upper(), orderId=int(order_id))
            )
        except Exception as e:
            logger.error(f"Error fetching order {order_id} for {symbol}: {e}")
            return None

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        """True if the order is now gone (cancelled or already inactive)."""
        if self.is_paper or not order_id or str(order_id).startswith("PAPER"):
            return True
        try:
            client = self._get_client()
            if not client:
                return False
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                self._executor, lambda: client.cancel_order(symbol=symbol.upper(), orderId=int(order_id))
            )
            return True
        except Exception as e:
            order = await self.get_order(symbol, order_id)
            if order and order.get("status") in ("CANCELED", "EXPIRED", "FILLED", "REJECTED"):
                return order.get("status") != "FILLED"
            logger.error(f"Error cancelling order {order_id}: {e}")
            return False

    async def get_asset_total(self, asset: str) -> Optional[float]:
        """free + locked balance (locked includes coins reserved by an open stop order). None on error."""
        if self.is_paper:
            return None
        try:
            client = self._get_client()
            if not client:
                return None
            loop = asyncio.get_running_loop()
            res = await loop.run_in_executor(
                self._executor, lambda: client.get_asset_balance(asset=asset.upper())
            )
            if res and "free" in res:
                return float(res["free"]) + float(res.get("locked", 0.0))
            return 0.0
        except Exception as e:
            logger.error(f"Error fetching total {asset} balance: {e}")
            return None

    # ── Simple Earn (Flexible) — live mode only; every method returns None/False on any problem ──
    async def _sapi(self, method: str, path: str, **data) -> Optional[Any]:
        client = self._get_client()
        if not client:
            return None
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._executor, lambda: client._request_margin_api(method, path, True, data=data)
        )

    async def earn_product(self, asset: str = "USDT") -> Optional[Dict[str, Any]]:
        """Best purchasable Flexible product for `asset` (highest advertised APR), or None."""
        try:
            res = await self._sapi("get", "simple-earn/flexible/list", asset=asset, size=100)
            rows = [r for r in (res or {}).get("rows", []) if r.get("canPurchase") and r.get("canRedeem", True)]
            if not rows:
                return None
            return max(rows, key=lambda r: float(r.get("latestAnnualPercentageRate") or 0))
        except Exception as e:
            logger.warning(f"Earn product lookup failed: {e}")
            return None

    async def earn_balance(self, asset: str = "USDT") -> Optional[float]:
        """Total {asset} (principal + interest) held in Flexible Earn; None if unknown."""
        try:
            res = await self._sapi("get", "simple-earn/flexible/position", asset=asset, size=100)
            if res is None:
                return None
            return sum(float(r.get("totalAmount") or 0) for r in res.get("rows", []))
        except Exception as e:
            logger.warning(f"Earn balance lookup failed: {e}")
            return None

    async def earn_subscribe(self, product_id: str, amount: float) -> bool:
        try:
            amt = format_to_step(amount, 0.01)
            if float(amt) <= 0:
                return False
            res = await self._sapi("post", "simple-earn/flexible/subscribe", productId=product_id,
                                   amount=amt, sourceAccount="SPOT")
            return bool(res and res.get("success", True))
        except Exception as e:
            logger.warning(f"Earn subscribe failed: {e}")
            return False

    async def earn_redeem(self, product_id: str, amount: float) -> bool:
        try:
            amt = format_to_step(amount, 0.01)
            if float(amt) <= 0:
                return False
            res = await self._sapi("post", "simple-earn/flexible/redeem", productId=product_id,
                                   amount=amt, destAccount="SPOT")
            return bool(res and res.get("success", True))
        except Exception as e:
            logger.warning(f"Earn redeem failed: {e}")
            return False

    async def split_symbol(self, symbol: str) -> tuple:
        """Return (base_asset, quote_asset). Falls back to a USDT suffix heuristic."""
        symbol = symbol.upper()
        for quote in ("USDT", "USDC", "FDUSD", "BUSD", "BTC", "ETH", "BNB"):
            if symbol.endswith(quote) and len(symbol) > len(quote):
                return symbol[: -len(quote)], quote
        return symbol, "USDT"

    async def check_key_safety(self) -> tuple:
        """
        Returns (ok, message). Keys are rejected if they can trade-and-withdraw:
        a bot key must never have withdrawal permission.
        """
        client = self._get_client(for_orders=False)
        if not client:
            return False, "No keys supplied"
        try:
            loop = asyncio.get_running_loop()
            perms = await loop.run_in_executor(self._executor, client.get_api_key_permissions)
            if perms.get("enableWithdrawals"):
                return False, "Withdrawals are enabled on this key. Create a new key with withdrawals DISABLED."
            if not perms.get("enableSpotAndMarginTrading"):
                return False, "Spot trading is not enabled on this key."
            if not perms.get("ipRestrict"):
                return True, "Warning: no IP restriction on this key. Whitelist your server IP in Binance."
            return True, "ok"
        except Exception as e:
            logger.error(f"Key safety check failed: {e}")
            return False, "Could not verify key permissions with Binance."

    async def validate_keys(self) -> bool:
        """Validate Binance API credentials by requesting account permissions."""
        if not self.api_key or not self.secret_key:
            return False

        try:
            client = self._get_client(for_orders=False)
            if not client:
                return False
            loop = asyncio.get_running_loop()
            account = await loop.run_in_executor(self._executor, client.get_account)
            return bool(account and "canTrade" in account)
        except Exception as e:
            logger.error(f"Failed to validate Binance API keys: {e}")
            return False

    async def close(self) -> None:
        """Clean up HTTP session and worker threads."""
        try:
            if self._session and not self._session.closed:
                await self._session.close()
                self._session = None
        except Exception as e:
            logger.error(f"Error closing aiohttp session: {e}")

        try:
            self._executor.shutdown(wait=False)
        except Exception as e:
            logger.error(f"Error shutting down exchange executor: {e}")
