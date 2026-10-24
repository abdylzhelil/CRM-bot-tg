"""Выдача дней полного тарифа (бонусы, рефералы, оплата)."""
from datetime import timedelta

from app.models import User, now
from app.services.distribute import is_paid


def extend(user: User, delta: timedelta, max_ahead: timedelta | None = None) -> bool:
    """Продлить 💎 на delta. False — продлевать нечего (администратор, бессрочный тариф или достигнут потолок).

    max_ahead — потолок для бонусов: тариф не уходит дальше now + max_ahead (оплата потолком не ограничена).
    """
    if user.is_admin or (user.plan == "full" and user.plan_until is None):
        return False
    start = user.plan_until if is_paid(user) else now()
    end = start + timedelta(seconds=delta.total_seconds())
    if max_ahead is not None:
        end = min(end, now() + max_ahead)
        if end - start < timedelta(minutes=1):
            return False
    user.plan, user.plan_until = "full", end
    return True
