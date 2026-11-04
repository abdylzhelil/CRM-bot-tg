"""«Сторож»: бот раз в 5 минут отмечается во внешнем сервисе (например, healthchecks.io). Пропали отметки — сервис пишет владельцу.

Работает, даже если сервер упал целиком (чего бот сам о себе сообщить не может). Выключено, пока HEARTBEAT_URL пуст.
"""
import asyncio
import logging

import aiohttp

from app import config

log = logging.getLogger(__name__)
INTERVAL = 300


async def ping(url: str) -> bool:
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as http:
            async with http.get(url) as resp:
                return resp.status < 400
    except Exception:
        log.info("отметка «сторожу» не дошла")
        return False


async def heartbeat_loop() -> None:
    if not config.HEARTBEAT_URL:
        return
    while True:
        await ping(config.HEARTBEAT_URL)
        await asyncio.sleep(INTERVAL)
