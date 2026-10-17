"""Лицензия бота модерации для админов групп: за заявки из группы, с ручным управлением владельца.

Здесь только учёт и ответ по HTTP (GET /feed/license). Включать/выключать функции в самом боте модерации должен он сам,
запрашивая этот ответ; тот бот мы не меняем без отдельного разрешения владельца.
"""
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Group, now
from app.services import settings


def active(g: Group) -> bool:
    return g.license_until is not None and g.license_until > now() and not g.license_off


async def award(s: AsyncSession, g: Group) -> bool:
    """За каждые license_leads заявок группы — license_days дней лицензии. False — ничего не выдано."""
    if g is None or not g.submitted_by or not g.catalog or g.license_off:
        return False
    if await settings.get(s, "license_enabled") != "1":
        return False
    per, days = await settings.get_int(s, "license_leads"), await settings.get_int(s, "license_days")
    steps = (g.leads_count - g.license_rewarded) // per if per > 0 else 0
    if steps <= 0:
        return False
    g.license_rewarded += steps * per
    start = g.license_until if g.license_until and g.license_until > now() else now()
    g.license_until = start + timedelta(days=days * steps)
    await s.commit()
    return True


async def grant(s: AsyncSession, g: Group, days: int) -> None:
    start = g.license_until if g.license_until and g.license_until > now() else now()
    g.license_until, g.license_off = start + timedelta(days=days), False
    await s.commit()


async def toggle(s: AsyncSession, g: Group) -> bool:
    """Ручное включение/отключение (при отключении автовыдача не продлевает). Возвращает новое состояние «выключена»."""
    g.license_off = not g.license_off
    await s.commit()
    return g.license_off
