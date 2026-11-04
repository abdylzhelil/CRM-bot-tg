"""Сообщения пользователей владельцу: любой свободный текст в боте (ответы на рассылку, вопросы, жалобы) сохраняется
и приходит админу, а не теряется. Ответ владельца человек получает в этот же чат."""
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Feedback, User, now

DAILY_LIMIT = 20  # больше сообщений в сутки от одного человека не сохраняем (защита от флуда)
MAX_LEN = 3000


async def save(s: AsyncSession, user: User, text: str) -> Feedback | None:
    """None — человек уже написал слишком много за сутки. Коммит — на вызывающем."""
    today = await s.scalar(select(func.count()).select_from(Feedback).where(
        Feedback.user_id == user.tg_id, Feedback.created_at > now() - timedelta(days=1))) or 0
    if today >= DAILY_LIMIT:
        return None
    fb = Feedback(user_id=user.tg_id, text=text.strip()[:MAX_LEN])
    s.add(fb)
    await s.flush()
    return fb


async def unread_count(s: AsyncSession) -> int:
    return await s.scalar(select(func.count()).select_from(Feedback).where(~Feedback.read)) or 0


async def recent(s: AsyncSession, unread_only: bool, limit: int = 10) -> list[tuple[Feedback, User | None]]:
    q = select(Feedback, User).join(User, User.tg_id == Feedback.user_id, isouter=True)
    if unread_only:
        q = q.where(~Feedback.read)
    return list((await s.execute(q.order_by(Feedback.id.desc()).limit(limit))).all())


async def answer(s: AsyncSession, fb_id: int, text: str) -> Feedback | None:
    fb = await s.get(Feedback, fb_id)
    if fb is None:
        return None
    fb.answer, fb.answered_at, fb.read = text.strip()[:MAX_LEN], now(), True
    return fb
