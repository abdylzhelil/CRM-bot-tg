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
