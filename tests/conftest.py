import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("BOT_TOKEN", "123456:TEST")
os.environ.setdefault("OWNER_IDS", "1")

import sys  # noqa: E402
from types import SimpleNamespace  # noqa: E402

import pytest  # noqa: E402
from sqlalchemy import pool  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

ADMIN_ID = 1


class FakeBot:
    """Вместо Telegram: запоминает, кому и что бот отправил."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.blocked: set[int] = set()

    async def send_message(self, chat_id, text, **kwargs):
        if chat_id in self.blocked:
            from aiogram.exceptions import TelegramForbiddenError
            from aiogram.methods import SendMessage

            raise TelegramForbiddenError(method=SendMessage(chat_id=chat_id, text=text), message="blocked")
        self.sent.append((chat_id, text))
        return SimpleNamespace(message_id=len(self.sent))

    async def get_chat_member_count(self, chat_id):
        return 1500

    def texts(self, chat_id: int) -> list[str]:
        return [t for c, t in self.sent if c == chat_id]


@pytest.fixture
async def maker(monkeypatch):
    """Чистая база в памяти; Session подменён во всех модулях приложения; админ (ID 1) заведён."""
    import app.bot.setup  # noqa: F401  — загрузить все модули, где есть Session
    import app.feed.server  # noqa: F401
    import app.main  # noqa: F401
    from app.migrate import migrate_data
    from app.models import Base
    from app.seed import seed
    from app.services import rules

    engine = create_async_engine("sqlite+aiosqlite://", poolclass=pool.StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    for name, mod in list(sys.modules.items()):
        if name.startswith("app.") and hasattr(mod, "Session"):
            monkeypatch.setattr(mod, "Session", factory)
    rules.invalidate()
    async with factory() as s:
        await seed(s)
        await migrate_data(s)
    yield factory
    await engine.dispose()


@pytest.fixture
def tgbot(monkeypatch):
    from app.bot import notify

    bot = FakeBot()
    monkeypatch.setattr(notify, "_bot", bot)
    return bot


async def make_user(factory, tg_id, niches=("Сайты",), city="", plan="free", **extra):
    from sqlalchemy import select

    from app.models import Category, User, UserCategory

    async with factory() as s:
        u = User(tg_id=tg_id, name=f"user{tg_id}", city=city, plan=plan, onboarded=True, **extra)
        s.add(u)
        for name in niches:
            cid = await s.scalar(select(Category.id).where(Category.name == name, Category.parent_id.is_(None)))
            s.add(UserCategory(user_id=tg_id, category_id=cid))
        await s.commit()
        return u
