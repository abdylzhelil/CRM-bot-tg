"""Реклама для админов групп: тратят заявки из своей группы и получают N дней «Рекомендуем» (каталог и канал статистики).

Заявки здесь — «валюта» помимо бонусных дней 💎 (одна заявка может приносить и дни, и рекламу). Рекламу подтверждает владелец.
"""
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Group, now
from app.services import settings


class AdError(Exception):
    """Понятная причина отказа."""


def active(g: Group) -> bool:
    return g.ad_until is not None and g.ad_until > now()


def available(g: Group) -> int:
    """Сколько заявок группы ещё не потрачено на рекламу."""
    return max(g.leads_count - g.ad_spent, 0)


async def request(s: AsyncSession, user_id: int, gid: int) -> Group:
    """Админ группы просит рекламу. Заявки списываются при подтверждении владельцем."""
    g = await s.get(Group, gid)
    if g is None or g.submitted_by != user_id or not g.catalog:
        raise AdError("Рекламу можно заказать только для своей группы из каталога.")
    if g.ad_pending:
        raise AdError("Заявка на рекламу уже отправлена — ждите ответа.")
    cost = await settings.get_int(s, "ad_leads")
    if cost <= 0:
        raise AdError("Реклама сейчас не подключена.")
    if available(g) < cost:
        raise AdError(f"Нужно {cost} заявок из этой группы, доступно {available(g)}.")
    g.ad_pending = True
    await s.commit()
    return g


async def approve(s: AsyncSession, g: Group) -> None:
    cost, days = await settings.get_int(s, "ad_leads"), await settings.get_int(s, "ad_days")
    start = g.ad_until if active(g) else now()
    g.ad_until, g.ad_spent, g.ad_pending = start + timedelta(days=days), g.ad_spent + cost, False
    await s.commit()


async def reject(s: AsyncSession, g: Group) -> None:
    g.ad_pending = False
    await s.commit()


async def featured(s: AsyncSession, limit: int = 3) -> list[Group]:
    rows = (await s.scalars(select(Group).where(Group.catalog, Group.ad_until > now())
                            .order_by(Group.ad_until.desc()).limit(limit))).all()
    return list(rows)
