"""Автоудаление «пустых» аккаунтов: зашли, но так и не завершили регистрацию."""
import logging
from datetime import timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Client, Delivery, SpamVote, User, UserCategory, UserNegative, now
from app.services import settings

log = logging.getLogger(__name__)


async def empty_users(s: AsyncSession, days: int) -> list[int]:
    """Не завершили регистрацию дольше days дней, заявок и клиентов нет, не администраторы."""
    cutoff = now() - timedelta(days=days)
    users = (await s.scalars(select(User).where(~User.onboarded, ~User.is_admin, User.created_at < cutoff))).all()
    out = []
    for x in users:
        has = await s.scalar(select(func.count()).select_from(Delivery).where(Delivery.user_id == x.tg_id)) or 0
        has += await s.scalar(select(func.count()).select_from(Client).where(Client.user_id == x.tg_id)) or 0
        if not has:
            out.append(x.tg_id)
    return out


async def remove(s: AsyncSession, ids: list[int]) -> None:
    if not ids:
        return
    for table in (UserCategory, UserNegative, SpamVote):
        await s.execute(delete(table).where(table.user_id.in_(ids)))
    await s.execute(delete(User).where(User.tg_id.in_(ids)))
    await s.commit()


async def auto_cleanup(s: AsyncSession) -> int:
    """Удалить пустые аккаунты старше cleanup_days (0 — выключено). Возвращает число удалённых."""
    days = await settings.get_int(s, "cleanup_days")
    if days <= 0:
        return 0
    ids = await empty_users(s, days)
    await remove(s, ids)
    return len(ids)
