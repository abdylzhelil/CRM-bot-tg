"""Выдача дней полного тарифа (бонусы, рефералы)."""
from datetime import timedelta

from app.models import User, now
from app.services.distribute import is_paid


def extend(user: User, delta: timedelta) -> bool:
    """Продлить 💎 на delta. False — продлевать нечего (администратор или бессрочный тариф)."""
    if user.is_admin or (user.plan == "full" and user.plan_until is None):
        return False
    start = user.plan_until if is_paid(user) else now()
    user.plan, user.plan_until = "full", start + timedelta(seconds=delta.total_seconds())
    return True
