"""Ping our own public URL so Render's free tier does not put the service to sleep (best effort)."""
import asyncio
import logging

import aiohttp

from app.config import Config

logger = logging.getLogger(__name__)


async def ping_once(url: str) -> bool:
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
            async with s.get(url.rstrip("/") + "/health") as r:
                return r.status == 200
    except Exception as e:
        logger.debug(f"Keepalive ping failed: {e}")
        return False


async def run() -> None:
    url = Config.WEBHOOK_URL
    if not url:
        return
    logger.info(f"Keepalive enabled: pinging {url}/health every {Config.KEEPALIVE_MINUTES} min")
    while True:
        await asyncio.sleep(max(Config.KEEPALIVE_MINUTES, 1) * 60)
        await ping_once(url)
