"""Оплата тарифа: пакеты дней, запись платежа, продление 💎, доля пригласившему."""
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import config
from app.models import Payment, User
from app.services import plans, referrals, settings

PACKAGES = (30, 90, 365)


def enabled() -> bool:
    return bool(config.PAYMENT_PROVIDER_TOKEN)


async def packages(s: AsyncSession) -> list[tuple[int, int]]:
    """[(дней, цена в ₽)] — только те, у которых цена задана."""
    out = [(d, await settings.get_int(s, f"price_{d}")) for d in PACKAGES]
    return [(d, p) for d, p in out if p > 0]


async def price_of(s: AsyncSession, days: int) -> int:
    return dict(await packages(s)).get(days, 0)


async def apply(s: AsyncSession, user_id: int, days: int, amount: int, currency: str,
                charge_id: str) -> tuple[bool, tuple[int, timedelta] | None]:
    """Записать платёж и продлить тариф. (новый ли платёж, доля пригласившему)."""
    if await s.scalar(select(Payment.id).where(Payment.charge_id == charge_id)):
        return False, None  # Telegram прислал повтор
    s.add(Payment(user_id=user_id, days=days, amount=amount, currency=currency, charge_id=charge_id))
    user = await s.get(User, user_id)
    bought = timedelta(days=days)
    plans.extend(user, bought)
    share = await referrals.share_of_bonus(s, user, bought)
    await s.commit()
    return True, share
