import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import Base, BlacklistEntry
from app.seed import seed
from app.services import rules
from app.services.antispam import AuthorInfo
from app.services.pipeline import Incoming, process


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    rules.invalidate()
    async with async_sessionmaker(engine, expire_on_commit=False)() as s:
        await seed(s)
        yield s


def msg(text, uid=1, group=10, **kw):
    a = AuthorInfo(user_id=uid, username=f"u{uid}", first_name="Иван", **kw)
    return Incoming(text=text, author=a, group_tg_id=group, group_title="Бизнес", message_id=1)


async def test_lead_created_and_dedup(session):
    lead = await process(session, msg("Ищу разработчика для лендинга, бюджет 10к"))
    assert lead and lead.category_id
    # тот же автор, та же заявка из другой группы — один раз
    assert await process(session, msg("Ищу разработчика для лендинга, бюджет 10к", group=11)) is None


async def test_negative_and_non_lead(session):
    assert await process(session, msg("Делаю сайты недорого")) is None
    assert await process(session, msg("Всем привет")) is None


async def test_blacklist(session):
    session.add(BlacklistEntry(user_id=5, status="spam"))
    await session.commit()
    assert await process(session, msg("Ищу сайт", uid=5)) is None


async def test_spammer_gets_blacklisted(session):
    spam = msg("Ищу сайт! Крипта и казино без вложений t.me/x пиши в лс", uid=8_100_000_000,
               has_photo=False, bot=True)
    assert await process(session, spam) is None
    assert (await session.get(BlacklistEntry, 8_100_000_000)).status == "spam"
