"""🔕 Уведомления о заявках выключены на время: заявки по-прежнему раздаются, но приходят без сообщения —
копятся в списке «🔕 Накопилось» (📥 Заявки). Человеку при заходе в бота напоминаем, что выключено;
когда срок кончился — уведомления включаются сами и приходит итог «накопилось N».

Это не «⏸ Пауза» (на паузе заявки не раздаются вовсе) и не «🌙 Тишина» (ночные часы, заявки придут утром сообщениями).
"""
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Delivery, User, now
from app.services import quiet

FOREVER = timedelta(days=3650)  # «пока не включу»
OPTIONS = [  # (код, подпись)
    ("1h", "1 час"), ("3h", "3 часа"), ("8h", "8 часов"), ("morning", "до утра (9:00)"),
    ("1d", "сутки"), ("3d", "3 дня"), ("7d", "неделю"), ("off", "пока не включу"),
]
_DELTA = {"1h": timedelta(hours=1), "3h": timedelta(hours=3), "8h": timedelta(hours=8), "1d": timedelta(days=1),
          "3d": timedelta(days=3), "7d": timedelta(days=7), "off": FOREVER}
REMIND_EVERY = timedelta(hours=1)  # напоминание при заходе в бота — не чаще


def is_muted(user: User, at: datetime | None = None) -> bool:
    return user.mute_until is not None and user.mute_until > (at or now())


def until_for(code: str, at: datetime | None = None) -> datetime:
    at = at or now()
    if code == "morning":  # ближайшие 9:00 по местному времени
        local = quiet.local_now()
        target = local.replace(hour=9, minute=0, second=0, microsecond=0)
        if target <= local:
            target += timedelta(days=1)
        return at + (target - local)
    if code not in _DELTA:
        raise ValueError(code)
    return at + _DELTA[code]


def until_text(user: User) -> str:
    if user.mute_until is None:
        return ""
    if user.mute_until - now() > timedelta(days=365):
        return "пока не включите"
    return "до " + quiet.to_local(user.mute_until).strftime("%d.%m %H:%M")


async def accumulated(s: AsyncSession, user_id: int) -> int:
    """Сколько заявок пришло без уведомления и ещё не разобрано."""
    return await s.scalar(select(func.count()).select_from(Delivery).where(
        Delivery.user_id == user_id, Delivery.muted, Delivery.sent, Delivery.status == "new")) or 0


async def set_mute(s: AsyncSession, user: User, code: str) -> datetime:
    """Выключить уведомления (коммит — на вызывающем)."""
    row = await s.get(User, user.tg_id)
    row.mute_until = until_for(code)
    row.mute_noted_at = now()  # только что сам выключил — сразу не напоминаем
    user.mute_until = row.mute_until
    return row.mute_until


async def unmute(s: AsyncSession, user: User) -> int:
    """Включить уведомления. Возвращает, сколько накопилось (коммит — на вызывающем)."""
    row = await s.get(User, user.tg_id)
    row.mute_until = None
    user.mute_until = None
    return await accumulated(s, user.tg_id)


async def expired(s: AsyncSession) -> list[User]:
    """У кого срок кончился: уведомления включаются (коммит — на вызывающем)."""
    rows = (await s.scalars(select(User).where(User.mute_until.is_not(None), User.mute_until <= now()))).all()
    for u in rows:
        u.mute_until = None
    return list(rows)


def needs_reminder(user: User) -> bool:
    return is_muted(user) and (user.mute_noted_at is None or user.mute_noted_at < now() - REMIND_EVERY)
