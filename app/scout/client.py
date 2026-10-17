from urllib.parse import urlparse

from telethon import TelegramClient
from telethon.sessions import StringSession

from app.models import ScoutAccount


def parse_proxy(url: str):
    """socks5://user:pass@host:port | http://host:port -> кортеж для Telethon."""
    if not url:
        return None
    u = urlparse(url)
    scheme = {"socks5": "socks5", "socks4": "socks4", "http": "http"}.get(u.scheme)
    if not scheme or not u.hostname or not u.port:
        raise ValueError(f"Неверный прокси: {url}")
    return (scheme, u.hostname, u.port, True, u.username, u.password)


def make_client(acc: ScoutAccount, session: str | None = None) -> TelegramClient:
    return TelegramClient(
        StringSession(acc.session if session is None else session),
        acc.api_id,
        acc.api_hash,
        proxy=parse_proxy(acc.proxy),
        auto_reconnect=True,
        connection_retries=None,  # переподключаться бесконечно
        retry_delay=5,
        device_model="Desktop",
    )


async def ensure_connected(client, timeout: float = 30) -> bool:
    """Связь с Telegram перед действием. Обрыв бывает (перезапуск, сеть) — переподключаемся, а не тратим запросы впустую."""
    import asyncio
    import logging

    if client.is_connected():
        return True
    try:
        await asyncio.wait_for(client.connect(), timeout)
    except Exception:
        logging.getLogger(__name__).warning("разведчик: не удалось переподключиться", exc_info=True)
    return client.is_connected()
