"""Администратор: смена тарифа пользователю — общая логика для бота и мини-приложения."""
from datetime import timedelta

from app.models import User, now
from app.services.distribute import is_paid


class PlanError(ValueError):
    pass


def set_plan(u: User, value: str) -> str:
    """value: «free» — бесплатный, «0» — бессрочный 💎, число — продлить 💎 на столько дней. Возвращает текст уведомления человеку."""
    if value == "free":
        u.plan, u.plan_until = "free", None
        return "Ваш тариф изменён на 🆓 Бесплатный."
    if not value.isdigit() or int(value) > 3650:
        raise PlanError("Срок: число дней (до 3650), 0 — бессрочно или free")
    days = int(value)
    start = u.plan_until if is_paid(u) and u.plan_until and u.plan_until > now() else now()
    u.plan, u.plan_until = "full", (start + timedelta(days=days)) if days else None
    return "🎉 Подключён 💎 Полный тариф" + (f" до {u.plan_until:%d.%m.%Y}" if u.plan_until else " бессрочно") \
        + ": заявки приходят сразу и без лимита."
