"""Бот в группе: сообщение с заявкой -> лид + уведомление владельцу; меню в группах не работает."""
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import Chat, Message, User
from sqlalchemy import pool, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.bot import notify
from app.bot.handlers import ingroup, leads
from app.bot.setup import OwnerOnly
from app.models import Base, Group, Lead
from app.seed import seed
from app.services import intake, rules

CHAT = Chat(id=-1001234567890, type="supergroup", title="Бизнес Махачкала")


@pytest.fixture
async def db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=pool.StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (ingroup, intake, notify, leads):
        monkeypatch.setattr(mod, "Session", maker)
    rules.invalidate()
    sent = AsyncMock()
    monkeypatch.setattr(notify, "_send", sent)
    async with maker() as s:
        await seed(s)
        yield s, sent


def group_msg(text: str, user_id: int = 42) -> Message:
    return Message(
        message_id=7, date=datetime.now(), chat=CHAT, text=text,
        from_user=User(id=user_id, is_bot=False, first_name="Марат", username="marat"),
    )


def fake_bot() -> MagicMock:
    bot = MagicMock()
    bot.get_chat_member_count = AsyncMock(return_value=1500)
    return bot


async def test_group_message_becomes_lead(db):
    s, sent = db
    await ingroup.group_message(group_msg("Ищу кто сделает лендинг для салона, бюджет 10к"), fake_bot())
    lead = await s.scalar(select(Lead))
    assert lead and lead.link == "https://t.me/c/1234567890/7" and lead.group_title == "Бизнес Махачкала"
    g = await s.scalar(select(Group).where(Group.tg_id == CHAT.id))
    assert g.status == "joined" and g.account_id is None and g.leads_count == 1
    card = [c.args[0] for c in sent.call_args_list if "Суть" in c.args[0]]
    assert card and "🔥" in card[0]


async def test_group_noise_ignored(db):
    s, _ = db
    await ingroup.group_message(group_msg("Всем привет, как дела?"), fake_bot())
    assert await s.scalar(select(Lead)) is None


async def test_owner_middleware_lets_groups_through_blocks_strangers():
    handler = AsyncMock(return_value="ok")
    mw = OwnerOnly()
    stranger = User(id=999, is_bot=False, first_name="x")
    assert await mw(handler, None, {"event_chat": CHAT, "event_from_user": stranger}) == "ok"
    private = Chat(id=999, type="private")
    assert await mw(handler, None, {"event_chat": private, "event_from_user": stranger}) is None
