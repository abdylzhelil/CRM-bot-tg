"""Бот как участник группы: читает сообщения и ловит заявки без аккаунта-разведчика.

Бот видит все сообщения группы, только если у него выключен privacy mode
(@BotFather → /setprivacy → Disable) или он администратор группы.
В группах бот ничего не пишет.
"""
import logging

from aiogram import Bot, F, Router
from aiogram.filters import ChatMemberUpdatedFilter, JOIN_TRANSITION, LEAVE_TRANSITION
from aiogram.types import Chat, ChatMemberUpdated, Message
from sqlalchemy import select

from app.bot import notify
from app.bot.common import esc
from app.db import Session
from app.feed import links
from app.models import Group, now
from app.services import intake
from app.services.antispam import AuthorInfo
from app.services.pipeline import Incoming

log = logging.getLogger(__name__)
router = Router()
router.message.filter(F.chat.type.in_({"group", "supergroup"}))
router.edited_message.filter(F.chat.type.in_({"group", "supergroup"}))

PRIVACY_HINT = (
    "⚠️ Сейчас бот видит в группах только команды. Чтобы он читал все сообщения:\n"
    "@BotFather → /setprivacy → выберите бота → <b>Disable</b>, затем удалите бота из группы "
    "и добавьте снова. Либо сделайте бота администратором группы."
)


def message_link(chat: Chat, message_id: int) -> str:
    return links.message_link(chat.id, chat.username, message_id)


async def upsert_group(bot: Bot, chat: Chat, status: str = "joined") -> tuple[Group, bool]:
    """Запомнить группу; True — если её раньше не было (или бот вернулся)."""
    async with Session() as s:
        g = await s.scalar(select(Group).where(Group.tg_id == chat.id))
        new = g is None or g.status != status
        if g is None:
            g = Group(tg_id=chat.id)
            s.add(g)
        g.title = chat.title or g.title
        g.username = chat.username or ""
        if new and status == "joined":
            g.joined_at = now()
            try:
                g.members = await bot.get_chat_member_count(chat.id)
            except Exception:
                pass
        g.status = status
        await s.commit()
        return g, new


async def reads_all(bot: Bot, chat_id: int) -> bool:
    me = await bot.me()
    if me.can_read_all_group_messages:
        return True
    try:
        member = await bot.get_chat_member(chat_id, me.id)
        return member.status in ("administrator", "creator")
    except Exception:
        return False


@router.my_chat_member(ChatMemberUpdatedFilter(JOIN_TRANSITION))
async def bot_added(event: ChatMemberUpdated, bot: Bot) -> None:
    if event.chat.type not in ("group", "supergroup"):
        return
    g, _ = await upsert_group(bot, event.chat)
    who = event.from_user.full_name if event.from_user else "—"
    text = (
        f"✅ Бот добавлен в группу «{esc(g.title)}» ({g.members} участников), добавил: {esc(who)}.\n"
        "Заявки из неё будут приходить сюда."
    )
    if not await reads_all(bot, event.chat.id):
        text += "\n\n" + PRIVACY_HINT
    await notify._send(text)


@router.my_chat_member(ChatMemberUpdatedFilter(LEAVE_TRANSITION))
async def bot_removed(event: ChatMemberUpdated, bot: Bot) -> None:
    if event.chat.type not in ("group", "supergroup"):
        return
    g, _ = await upsert_group(bot, event.chat, status="left")
    await notify._send(f"➖ Бота удалили из группы «{esc(g.title)}».")


@router.message()
@router.edited_message()
async def group_message(m: Message, bot: Bot) -> None:
    text = m.text or m.caption or ""
    user = m.from_user
    # анонимные админы и посты каналов приходят от имени чата — их пропускаем
    if not text or len(text) < 8 or not user or m.sender_chat:
        return
    try:
        _, new = await upsert_group(bot, m.chat)
        if new:  # группа появилась без события о добавлении (например, после перезапуска)
            await notify._send(f"✅ Бот читает группу «{esc(m.chat.title or '')}».")
        await intake.handle(Incoming(
            text=text,
            author=AuthorInfo(
                user_id=user.id,
                username=user.username or "",
                first_name=user.first_name or "",
                last_name=user.last_name or "",
                bot=user.is_bot,
            ),
            group_tg_id=m.chat.id,
            group_title=m.chat.title or "",
            message_id=m.message_id,
            link=message_link(m.chat, m.message_id),
        ))
    except Exception:
        log.exception("сообщение из группы %s", m.chat.id)
