"""Публичная статистика для канала: только цифры, без имён, ников и текстов заявок."""
import logging
from datetime import timedelta

from sqlalchemy import func, select

from app import config
from app.db import Session
from app.models import Category, Group, Lead, User, now
from app.services import quiet, settings
from app.services.distribute import root_category

log = logging.getLogger(__name__)


async def build() -> str:
    async with Session() as s:
        day, week = now() - timedelta(days=1), now() - timedelta(days=7)
        leads_day = await s.scalar(select(func.count()).select_from(Lead).where(Lead.created_at > day)) or 0
        leads_week = await s.scalar(select(func.count()).select_from(Lead).where(Lead.created_at > week)) or 0
        users = await s.scalar(select(func.count()).select_from(User).where(User.onboarded)) or 0
        groups = await s.scalar(select(func.count()).select_from(Group).where(Group.status == "joined")) or 0
        catalog = await s.scalar(select(func.count()).select_from(Group).where(Group.catalog)) or 0
        niches: dict[int, int] = {}
        for cid, n in (await s.execute(select(Lead.category_id, func.count()).where(Lead.created_at > week)
                                       .group_by(Lead.category_id))).all():
            root = await root_category(s, cid)
            if root:
                niches[root] = niches.get(root, 0) + n
        top = []
        for root, n in sorted(niches.items(), key=lambda kv: -kv[1])[:3]:
            cat = await s.get(Category, root)
            top.append(f"• {cat.emoji} {cat.name}: {n}".replace("  ", " "))
        cities = (await s.execute(select(Lead.city, func.count()).where(Lead.created_at > week, Lead.city != "")
                                  .group_by(Lead.city).order_by(func.count().desc()).limit(3))).all()
    lines = [
        "📊 <b>Статистика за сутки</b>",
        f"Найдено заявок: {leads_day} (за неделю — {leads_week})",
        f"Пользователей в боте: {users}",
        f"Групп и каналов под наблюдением: {groups}, в каталоге: {catalog}",
    ]
    if top:
        lines += ["", "Самые востребованные ниши недели:"] + top
    if cities:
        lines += ["", "Города с наибольшим спросом: " + ", ".join(f"{c} ({n})" for c, n in cities)]
    return "\n".join(lines)


async def publish(bot) -> bool:
    """Отправить статистику в канал. False — канал не задан или бот не смог написать."""
    if not config.STATS_CHANNEL:
        return False
    chat = int(config.STATS_CHANNEL) if config.STATS_CHANNEL.lstrip("-").isdigit() else config.STATS_CHANNEL
    try:
        await bot.send_message(chat, await build())
        return True
    except Exception:
        log.warning("не удалось опубликовать статистику в канал", exc_info=True)
        return False


async def stats_loop(bot) -> None:
    """Раз в сутки в нужный час."""
    import asyncio

    last = None
    while True:
        try:
            t = quiet.local_now()
            async with Session() as s:
                hour = await settings.get_int(s, "channel_hour")
            if config.STATS_CHANNEL and t.hour == hour and last != t.date():
                last = t.date()
                await publish(bot)
        except Exception:
            log.exception("stats_loop")
        await asyncio.sleep(60)
