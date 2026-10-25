"""Автоудаление «пустых» аккаунтов: зашли, но так и не завершили регистрацию.

Порядок: за cleanup_warn_days до срока человеку приходит предупреждение с кнопкой «Продолжить», и только потом (когда
этот срок прошёл) аккаунт удаляется. Тем, кто заблокировал бота, предупреждать некуда — их удаляем по сроку.
"""
import logging
from datetime import timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.common import kb
from app.models import (
    AdOrder, Client, CreditTx, Delivery, Group, Payment, SpamVote, User, UserCategory, UserNegative, now,
)
from app.services import settings

log = logging.getLogger(__name__)


async def empty_users(s: AsyncSession, days: int) -> list[User]:
    """Не завершили регистрацию дольше days дней, заявок, клиентов, оплат, заказов рекламы, кредитов и групп с нашим
    ботом нет, не администраторы."""
    cutoff = now() - timedelta(days=days)
    users = (await s.scalars(select(User).where(~User.onboarded, ~User.is_admin, User.created_at < cutoff))).all()
    out = []
    for x in users:
        has = await s.scalar(select(func.count()).select_from(Delivery).where(Delivery.user_id == x.tg_id)) or 0
        has += await s.scalar(select(func.count()).select_from(Client).where(Client.user_id == x.tg_id)) or 0
        # заплатившего или заказавшего рекламу не удаляем: по оплате может понадобиться возврат
        has += await s.scalar(select(func.count()).select_from(Payment).where(Payment.user_id == x.tg_id)) or 0
        has += await s.scalar(select(func.count()).select_from(AdOrder).where(AdOrder.user_id == x.tg_id)) or 0
        # админ группы с нашим ботом (ему идут кредиты) и любые кредиты — не «пустой» аккаунт
        has += (x.credits or 0) + (x.credits_hold or 0)
        has += await s.scalar(select(func.count()).select_from(CreditTx).where(CreditTx.user_id == x.tg_id)) or 0
        has += await s.scalar(select(func.count()).select_from(Group).where(Group.board_admin == x.tg_id)) or 0
        if not has:
            out.append(x)
    return out


async def blocked_users(s: AsyncSession, days: int) -> list[User]:
    """Заблокировали бота и не заходили дольше days дней. Не трогаем админов, тех, у кого оплаты, заказы рекламы, кредиты,
    группы с нашим ботом или клиенты в CRM (там их данные и деньги)."""
    cutoff = now() - timedelta(days=days)
    users = (await s.scalars(select(User).where(User.blocked, ~User.is_admin, User.last_seen < cutoff))).all()
    out = []
    for x in users:
        has = (x.credits or 0) + (x.credits_hold or 0)
        for model in (Client, Payment, AdOrder, CreditTx):
            has += await s.scalar(select(func.count()).select_from(model).where(model.user_id == x.tg_id)) or 0
        has += await s.scalar(select(func.count()).select_from(Group).where(Group.board_admin == x.tg_id)) or 0
        if not has:
            out.append(x)
    return out


async def auto_blocked(s: AsyncSession) -> int:
    """Удалить заблокировавших бота через blocked_keep_days (0 — не удалять)."""
    from app.services import erase

    days = await settings.get_int(s, "blocked_keep_days")
    if days <= 0:
        return 0
    done = 0
    for u in await blocked_users(s, days):
        if await erase.erase_user(s, u.tg_id):
            done += 1
    return done


async def remove(s: AsyncSession, ids: list[int]) -> None:
    if not ids:
        return
    for table in (UserCategory, UserNegative, SpamVote):
        await s.execute(delete(table).where(table.user_id.in_(ids)))
    await s.execute(delete(User).where(User.tg_id.in_(ids)))
    await s.commit()


def warning_text(days_left: int) -> str:
    when = "завтра" if days_left <= 1 else f"через {days_left} дн."
    return ("👋 Вы начали настройку «КлиентРадара», но не закончили. "
            f"Чтобы не хранить лишнее, {when} мы удалим ваш аккаунт. "
            "Если бот вам нужен — нажмите кнопку и завершите настройку за 30 секунд.")


async def warn_due(s: AsyncSession, send) -> int:
    """Отправить предупреждения тем, кому пора. send(user_id, text, markup) -> bool (False — доставить нельзя)."""
    days = await settings.get_int(s, "cleanup_days")
    warn = await settings.get_int(s, "cleanup_warn_days")
    if days <= 0 or warn <= 0:
        return 0
    sent = 0
    for u in await empty_users(s, max(days - warn, 0)):
        if u.warned_at is not None or u.blocked:
            continue
        u.warned_at = now()  # отметка ставится в любом случае, чтобы не пытаться бесконечно
        if await send(u.tg_id, warning_text(warn), kb([[("▶️ Завершить настройку", "reg:go")]])):
            sent += 1
        else:
            u.blocked = True
    await s.commit()
    return sent


async def auto_cleanup(s: AsyncSession) -> int:
    """Удалить пустые аккаунты старше cleanup_days (0 — выключено), если предупреждение уже «отлежало» свой срок."""
    days = await settings.get_int(s, "cleanup_days")
    if days <= 0:
        return 0
    warn = await settings.get_int(s, "cleanup_warn_days")
    ready = []
    for u in await empty_users(s, days):
        warned_long_ago = u.warned_at is not None and u.warned_at <= now() - timedelta(days=warn)
        if warn <= 0 or u.blocked or warned_long_ago:
            ready.append(u.tg_id)
    await remove(s, ready)
    return len(ready)
