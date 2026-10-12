"""Рефералы: приглашённому и пригласившему по N дней 💎, плюс процент от бонусных дней приглашённого."""
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User
from app.services import plans, settings


def link(bot_username: str, tg_id: int) -> str:
    return f"https://t.me/{bot_username}?start=ref_{tg_id}"


def parse_payload(payload: str | None) -> int | None:
    if payload and payload.startswith("ref_") and payload[4:].isdigit():
        return int(payload[4:])
    return None


async def attach(s: AsyncSession, user: User, referrer_id: int) -> bool:
    """Запомнить, кто пригласил. Только для ещё не зарегистрированных и не самого себя."""
    if user.onboarded or user.referred_by or referrer_id == user.tg_id:
        return False
    if await s.get(User, referrer_id) is None:
        return False
    row = await s.get(User, user.tg_id)
    row.referred_by = user.referred_by = referrer_id
    await s.commit()
    return True


async def reward_signup(s: AsyncSession, user: User) -> int | None:
    """Приглашённый завершил регистрацию: по ref_days обоим. Возвращает ID пригласившего."""
    row = await s.get(User, user.tg_id)
    if not row.onboarded or not row.referred_by or row.ref_rewarded:
        return None
    referrer = await s.get(User, row.referred_by)
    row.ref_rewarded = True
    if referrer is None:
        await s.commit()
        return None
    if await _rewarded_count(s, referrer.tg_id) > await settings.get_int(s, "ref_limit"):
        await s.commit()  # лимит приглашений исчерпан — награды нет ни у кого (защита от фермы аккаунтов)
        return None
    delta = timedelta(days=await settings.get_int(s, "ref_days"))
    cap = timedelta(days=await settings.get_int(s, "bonus_max_days"))
    plans.extend(row, delta, cap)
    plans.extend(referrer, delta, cap)
    await s.commit()
    return referrer.tg_id


async def _rewarded_count(s: AsyncSession, referrer_id: int) -> int:
    return await s.scalar(select(func.count()).select_from(User).where(
        User.referred_by == referrer_id, User.ref_rewarded)) or 0


async def share_of_bonus(s: AsyncSession, user: User, earned: timedelta) -> tuple[int, timedelta] | None:
    """Процент от бонуса приглашённого уходит пригласившему (выдаёт, если у того есть что продлевать)."""
    if not user.referred_by or not user.ref_rewarded:
        return None
    referrer = await s.get(User, user.referred_by)
    percent = await settings.get_int(s, "ref_percent")
    share = earned * percent / 100
    cap = timedelta(days=await settings.get_int(s, "bonus_max_days"))
    if referrer is None or share <= timedelta(0) or not plans.extend(referrer, share, cap):
        return None
    return referrer.tg_id, share


async def stats(s: AsyncSession, user_id: int) -> tuple[int, int]:
    """(приглашено всего, из них завершили регистрацию)."""
    total = await s.scalar(select(func.count()).select_from(User).where(User.referred_by == user_id)) or 0
    done = await s.scalar(select(func.count()).select_from(User).where(
        User.referred_by == user_id, User.ref_rewarded)) or 0
    return total, done
