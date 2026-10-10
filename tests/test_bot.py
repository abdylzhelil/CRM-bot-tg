"""Дымовые тесты хендлеров бота (без сети): вызываем функции напрямую с моками."""
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.bot import notify
from app.bot.handlers import inputs, leads, stats
from app.models import Base, BlacklistEntry, Client, Keyword, Lead
from app.seed import seed


@pytest.fixture
async def db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=__import__("sqlalchemy").pool.StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (leads, stats):
        monkeypatch.setattr(mod, "Session", maker)
    async with maker() as s:
        await seed(s)
        yield s


def cb(data):
    c = MagicMock()
    c.data = data
    c.answer, c.message.edit_reply_markup = AsyncMock(), AsyncMock()
    return c


async def test_take_and_spam_lead(db):
    lead = Lead(author_id=7, username="vasya", name="Вася", text="Ищу лендинг", text_hash="h", group_title="G")
    db.add(lead)
    await db.commit()

    await leads.lead_action(cb(f"lead:take:{lead.id}"))
    client = await db.scalar(select(Client).where(Client.tg_id == 7))
    assert client and client.stage == "new"

    await leads.lead_action(cb(f"lead:spam:{lead.id}"))
    assert (await db.get(BlacklistEntry, 7)).status == "spam"


async def test_keyword_edit_via_input(db):
    m = AsyncMock()
    await inputs.kw_add(db, m, {"kind": "intent", "cid": 0}, "хочу, надо")
    assert await db.scalar(select(Keyword).where(Keyword.word == "надо"))
    await inputs.kw_del(db, m, {"kind": "intent", "cid": 0}, "надо")
    assert await db.scalar(select(Keyword).where(Keyword.word == "надо")) is None


async def test_report_and_notify_text(db):
    assert "Заявок" in await stats.build_report()
    lead = Lead(id=1, author_id=1, username="u", name="N", text="<b>x</b>", text_hash="h", group_title="G", link="https://t.me/g/1")
    text = notify.lead_text(lead, "💻 Сайты")
    assert "&lt;b&gt;" in text and "Новый клиент" in text
