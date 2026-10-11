"""Бот в группе: сообщение с заявкой -> лид + карточка; посторонние в личке регистрируются как пользователи."""
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

from aiogram.types import Chat, Message, User
from sqlalchemy import select

from app.bot.access import IsAdmin, UserMiddleware
from app.bot.handlers import ingroup
from app.models import Group, Lead
from app.models import User as DbUser
from tests.conftest import ADMIN_ID

CHAT = Chat(id=-1001234567890, type="supergroup", title="Бизнес Махачкала")


def group_msg(text: str, user_id: int = 42) -> Message:
    return Message(
        message_id=7, date=datetime.now(), chat=CHAT, text=text,
        from_user=User(id=user_id, is_bot=False, first_name="Марат", username="marat"),
    )


def fake_bot() -> MagicMock:
    bot = MagicMock()
    bot.get_chat_member_count = AsyncMock(return_value=1500)
    return bot


async def test_group_message_becomes_lead(maker, tgbot):
    await ingroup.group_message(group_msg("Ищу кто сделает лендинг для салона, бюджет 10к"), fake_bot())
    async with maker() as s:
        lead = await s.scalar(select(Lead))
        g = await s.scalar(select(Group).where(Group.tg_id == CHAT.id))
    assert lead and lead.link == "https://t.me/c/1234567890/7" and lead.city == "Махачкала"
    assert g.status == "joined" and g.account_id is None and g.leads_count == 1
    cards = [t for t in tgbot.texts(ADMIN_ID) if "Суть" in t]
    assert cards and "🔥" in cards[0]


async def test_group_noise_ignored(maker, tgbot):
    await ingroup.group_message(group_msg("Всем привет, как дела?"), fake_bot())
    async with maker() as s:
        assert await s.scalar(select(Lead)) is None


async def test_middleware_registers_users_and_admin_flag(maker):
    handler = AsyncMock(return_value="ok")
    mw = UserMiddleware()
    stranger = User(id=999, is_bot=False, first_name="Гость")
    # сообщения групп проходят без регистрации
    assert await mw(handler, None, {"event_chat": CHAT, "event_from_user": stranger}) == "ok"
    data = {"event_chat": Chat(id=999, type="private"), "event_from_user": stranger}
    assert await mw(handler, None, data) == "ok"
    assert data["user"].tg_id == 999 and not data["user"].is_admin and not data["user"].onboarded
    assert not await IsAdmin()(None, user=data["user"])
    async with maker() as s:
        assert (await s.get(DbUser, 999)).name == "Гость"
    owner = {
        "event_chat": Chat(id=ADMIN_ID, type="private"),
        "event_from_user": User(id=ADMIN_ID, is_bot=False, first_name="A"),
    }
    await mw(handler, None, owner)
    assert await IsAdmin()(None, user=owner["user"])
