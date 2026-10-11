"""Поиск публичных групп по ключевым запросам."""
import asyncio
import logging
import random

from sqlalchemy import select
from telethon import TelegramClient, functions, utils
from telethon.errors import FloodWaitError
from telethon.tl.types import Channel

from app.bot import notify
from app.db import Session
from app.models import FeedChat, Group, SearchQuery
from app.services import settings

log = logging.getLogger(__name__)


def detect_lang(title: str) -> str:
    cyr = sum("а" <= c.lower() <= "я" or c.lower() == "ё" for c in title)
    return "ru" if cyr >= max(1, len(title) // 3) else "other"


async def _activity(client: TelegramClient, entity: Channel) -> float:
    """Сообщений в сутки по последним 100 сообщениям; -1 если прочитать нельзя."""
    try:
        msgs = [m async for m in client.iter_messages(entity, limit=100)]
    except Exception:
        return -1
    if len(msgs) < 2:
        return 0
    span = (msgs[0].date - msgs[-1].date).total_seconds() / 86400
    return len(msgs) / max(span, 1 / 24)


_stop_requested = False


def request_stop() -> None:
    """Кнопка «⏹ Остановить поиск»: текущий проход завершится после ближайшего шага."""
    global _stop_requested
    _stop_requested = True


async def is_covered(s, tg_id: int) -> bool:
    """Общая база: в группе уже есть наш бот (CRM-бот, бот модерации или другой разведчик)."""
    if await s.get(FeedChat, tg_id) is not None:
        return True
    g = await s.scalar(select(Group).where(Group.tg_id == tg_id))
    return g is not None and g.status == "joined"


async def discover(client: TelegramClient) -> int:
    """Один проход по всем включённым запросам. Возвращает число новых кандидатов.

    Карточки по одной не шлём — в конце одно сообщение «найдено N», отбор — списком в 🔍 Группы.
    """
    global _stop_requested
    _stop_requested = False
    async with Session() as s:
        queries = [q.text for q in (await s.scalars(select(SearchQuery).where(SearchQuery.enabled))).all()]
        min_members = await settings.get_int(s, "min_members")
        min_msgs = await settings.get_int(s, "min_msgs_per_day")
        auto = await settings.get(s, "mode") == "auto"
    found = 0
    for q in queries:
        if _stop_requested:
            break
        try:
            res = await client(functions.contacts.SearchRequest(q=q, limit=30))
        except FloodWaitError as e:
            log.warning("FloodWait %ss на поиске", e.seconds)
            await asyncio.sleep(e.seconds + 5)
            continue
        except Exception:
            log.exception("поиск «%s»", q)
            continue
        for chat in res.chats:
            if _stop_requested:
                break
            if not (isinstance(chat, Channel) and chat.megagroup):
                continue  # нужны группы/чаты, не каналы
            tg_id = utils.get_peer_id(chat)
            async with Session() as s:
                if await s.scalar(select(Group.id).where(Group.tg_id == tg_id)) or await is_covered(s, tg_id):
                    continue
            try:
                full = await client(functions.channels.GetFullChannelRequest(chat))
                members = full.full_chat.participants_count or 0
                msgs_day = await _activity(client, chat)
            except FloodWaitError as e:
                await asyncio.sleep(e.seconds + 5)
                continue
            except Exception:
                log.exception("данные группы %s", chat.title)
                continue
            ok = members >= min_members and (msgs_day < 0 or msgs_day >= min_msgs)
            async with Session() as s:
                g = Group(
                    tg_id=tg_id, username=chat.username or "", title=chat.title or "",
                    members=members, msgs_per_day=msgs_day, lang=detect_lang(chat.title or ""),
                    status=("approved" if auto else "candidate") if ok else "skipped",
                )
                s.add(g)
                await s.commit()
            if ok:
                found += 1
            await asyncio.sleep(random.uniform(2, 6))
        await asyncio.sleep(random.uniform(10, 30))
    if found:
        await notify.groups_found(found, auto)
    return found
