import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("BOT_TOKEN", "123456:TEST")
os.environ.setdefault("OWNER_IDS", "1")
