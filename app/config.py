import os

from dotenv import load_dotenv

load_dotenv()


def _ids(raw: str) -> set[int]:
    return {int(x) for x in raw.replace(" ", "").split(",") if x}


BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OWNER_IDS = _ids(os.getenv("OWNER_IDS", ""))
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///data/crm.db")
TZ_NAME = os.getenv("TZ_NAME", "Europe/Moscow")
TG_API_ID = int(os.getenv("TG_API_ID") or 0)
TG_API_HASH = os.getenv("TG_API_HASH", "")

# Приём сообщений от бота модерации (tg-group-bot). Пусто — приём выключен.
FEED_TOKEN = os.getenv("FEED_TOKEN", "")
FEED_PORT = int(os.getenv("FEED_PORT") or 8090)

# Онлайн-оплата через Telegram Payments (токен провайдера из @BotFather -> Payments). Пусто — оплата выключена.
PAYMENT_PROVIDER_TOKEN = os.getenv("PAYMENT_PROVIDER_TOKEN", "")
PAYMENT_CURRENCY = os.getenv("PAYMENT_CURRENCY", "RUB")
# Публичный канал со статистикой (@имя или -100…); бот должен быть его администратором. Пусто — выключено.
STATS_CHANNEL = os.getenv("STATS_CHANNEL", "")
# Возврат денег из бота (необязательно): shopId и секретный ключ из кабинета ЮKassa -> Интеграция -> Ключи API.
YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID", "")
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY", "")
