"""Напоминание самозанятому про чеки в «Мой налог»: до 9-го числа за оплаты прошлого месяца."""
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Payment, Setting
from app.services import quiet

REMIND_DAY = 5  # напоминаем 5-го числа, срок — 9-е


def month_bounds(today: datetime) -> tuple[datetime, datetime, str]:
    """Начало прошлого месяца, начало этого и ключ «ГГГГ-ММ» (по местной дате, точность до часов не нужна)."""
    end = datetime(today.year, today.month, 1)
    prev = end - timedelta(days=1)
    start = datetime(prev.year, prev.month, 1)
    return start, end, f"{start:%Y-%m}"


async def due_message(s: AsyncSession) -> str | None:
    """Текст напоминания, если пора и ещё не напоминали за этот месяц."""
    today = quiet.local_now()
    if today.day < REMIND_DAY or today.hour < 10:
        return None
    start, end, key = month_bounds(today)
    row = await s.get(Setting, "receipts_reminded")
    if row is not None and row.value == key:
        return None
    count, total = (await s.execute(select(func.count(), func.coalesce(func.sum(Payment.amount - Payment.refunded_amount), 0)).where(
        Payment.created_at >= start, Payment.created_at < end, ~Payment.refunded))).one()
    if row is None:
        row = Setting(key="receipts_reminded", value=key)
        s.add(row)
    row.value = key
    await s.commit()
    if not count:
        return None
    return (f"🧾 Чеки за {start:%m.%Y}: оплат {count} на {total / 100:.0f} ₽. "
            "Сформируйте чеки в приложении «Мой налог» до 9-го числа. Возвращённые платежи не считаются.")
