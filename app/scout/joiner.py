"""Вступление в группы с лимитами, случайными паузами и обработкой FloodWait; ротация."""
import asyncio
import logging
import random
from datetime import date, timedelta

from sqlalchemy import func, or_, select
from telethon import TelegramClient, functions
from telethon.errors import (
    ChannelsTooMuchError,
    ChannelPrivateError,
    FloodWaitError,
    InviteRequestSentError,
    UserAlreadyParticipantError,
)

from app.bot import notify
from app.db import Session
from app.models import FeedChat, Group, ScoutAccount, now
from app.services import settings

log = logging.getLogger(__name__)


async def is_covered_by_other(s, g: Group) -> bool:
    other = await s.scalar(select(Group.id).where(Group.tg_id == g.tg_id, Group.id != g.id, Group.status == "joined"))
    return other is not None


async def paused() -> bool:
    async with Session() as s:
        return await settings.get(s, "scout_paused") == "1"


async def join_one(client: TelegramClient, acc_id: int) -> bool:
    """Вступить в одну одобренную группу. True — если попытка была (нужна пауза)."""
    joined_entity = None
    async with Session() as s:
        acc = await s.get(ScoutAccount, acc_id)
        today = date.today().isoformat()
        if acc.joined_date != today:
            acc.joined_date, acc.joined_today = today, 0
        total = await s.scalar(
            select(func.count()).select_from(Group).where(Group.account_id == acc_id, Group.status == "joined")
        )
        if acc.joined_today >= acc.daily_join_limit or total >= acc.max_groups:
            await s.commit()
            return False
        g = await s.scalar(
            # IN (NULL, …) в SQL не находит NULL — поэтому явно: ничья группа или уже назначенная этому аккаунту
            select(Group).where(Group.status == "approved", Group.kind == "group",
                                or_(Group.account_id.is_(None), Group.account_id == acc_id))
            .order_by(Group.members.desc())
        )
        if not g:
            await s.commit()
            return False
        if await s.get(FeedChat, g.tg_id) is not None or await is_covered_by_other(s, g):
            g.status = "covered"  # там уже есть наш бот — второй не заходит
            await s.commit()
            return False
        try:
            entity = await client.get_entity(g.username or g.tg_id)
            await client(functions.channels.JoinChannelRequest(entity))
            g.status, g.account_id, g.joined_at = "joined", acc_id, now()
            acc.joined_today += 1
            joined_entity = entity
        except UserAlreadyParticipantError:
            g.status, g.account_id, g.joined_at = "joined", acc_id, now()
        except FloodWaitError as e:
            log.warning("FloodWait %ss при вступлении — жду", e.seconds)
            await s.commit()
            await asyncio.sleep(e.seconds + random.uniform(5, 30))
            return True
        except ChannelsTooMuchError:
            acc.joined_today = acc.daily_join_limit
            await notify.alert(f"Аккаунт «{acc.name}» достиг лимита групп Telegram. Запустите ротацию.")
        except (InviteRequestSentError, ChannelPrivateError):
            g.status = "skipped"
        except Exception:
            log.exception("вступление в %s", g.title)
            g.status = "skipped"
        await s.commit()
    if joined_entity is not None:
        from app.scout.folders import mute, sync_folders

        await mute(client, joined_entity)
        try:
            await sync_folders(client, acc_id)
        except Exception:
            log.warning("папки разведчика не обновлены", exc_info=True)
    return True


async def join_loop(manager) -> None:
    """Раз в цикл — по одному вступлению на аккаунт, затем случайная пауза."""
    while True:
        did = False
        for acc_id, client in list(manager.clients.items()):
            if await paused():
                break
            try:
                did |= await join_one(client, acc_id)
            except Exception:
                log.exception("join_loop #%s", acc_id)
        await asyncio.sleep(random.uniform(90, 420) if did else 60)


async def rotate(client: TelegramClient, acc_id: int, limit: int = 10) -> int:
    """Выйти из групп без заявок за N дней (освободить место для новых из очереди)."""
    async with Session() as s:
        days = await settings.get_int(s, "rotation_days")
        cutoff = now() - timedelta(days=days)
        stale = (await s.scalars(
            select(Group).where(
                Group.account_id == acc_id, Group.status == "joined", Group.joined_at < cutoff,
                (Group.last_lead_at.is_(None)) | (Group.last_lead_at < cutoff),
            ).limit(limit)
        )).all()
        left = 0
        for g in stale:
            try:
                await client(functions.channels.LeaveChannelRequest(await client.get_entity(g.tg_id)))
                g.status = "left"
                left += 1
            except FloodWaitError as e:
                await asyncio.sleep(e.seconds + 5)
            except Exception:
                log.exception("выход из %s", g.title)
            await asyncio.sleep(random.uniform(20, 90))
        await s.commit()
    return left


async def search_loop(manager) -> None:
    from app.scout.search import discover

    while True:
        for acc_id, client in list(manager.clients.items()):
            if await paused():
                break
            try:
                await discover(client)
            except Exception:
                log.exception("search_loop #%s", acc_id)
        async with Session() as s:
            hours = await settings.get_int(s, "search_interval_h")
        await asyncio.sleep(hours * 3600)


async def rotation_loop(manager) -> None:
    while True:
        await asyncio.sleep(7 * 86400)
        for acc_id, client in list(manager.clients.items()):
            try:
                await rotate(client, acc_id)
            except Exception:
                log.exception("rotation #%s", acc_id)
