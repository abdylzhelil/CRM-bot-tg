"""Проверка initData мини-приложения Telegram (подпись HMAC-SHA256 ключом, выведенным из токена бота)."""
import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl

MAX_AGE = 24 * 3600


def verify_init_data(init_data: str, bot_token: str, max_age: int = MAX_AGE, now: float | None = None) -> dict | None:
    """Возвращает данные пользователя Telegram (dict с id, first_name…) или None, если подпись неверна или данные устарели."""
    if not init_data or not bot_token:
        return None
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
    except ValueError:
        return None
    got = pairs.pop("hash", "")
    if not got:
        return None
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    want = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(want, got):
        return None
    try:
        issued = int(pairs.get("auth_date", "0"))
        user = json.loads(pairs.get("user", ""))
    except (ValueError, TypeError):
        return None
    if (now if now is not None else time.time()) - issued > max_age or not isinstance(user, dict) or "id" not in user:
        return None
    return user
