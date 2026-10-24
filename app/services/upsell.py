"""Оплата и польза 💎 (решение владельца 25.10): человек должен видеть, что теряет и что получает.

1. «Лимит исчерпан»: бесплатному, у которого сегодня кончился лимит, — раз в день, сколько заявок по его нишам
   ушло мимо (`users.missed_*`), с кнопкой 💎. После оплаты бот присылает заявки за последние сутки.
2. Цифры в «💎 Тариф»: сколько заявок было за неделю по нишам и городу человека и сколько пришло ему.
3. Пробный 💎 (`trial_days`) — один раз новому пользователю после регистрации (`users.trial_used/on_trial`).
4. Гарантия заявок (`guarantee_min`/`guarantee_days`): оплатившему, если за 7 дней пришло меньше N заявок, — дни 💎.
Деньги здесь не двигаются — только дни тарифа.
"""
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Category, Delivery, Lead, Payment, User, UserCategory, now
from app.services import geo, plans, quiet, settings

NOTE_AFTER = 3          # «лимит исчерпан» — когда мимо прошло уже столько заявок…
NOTE_EVENING_HOUR = 20  # …или вечером, если пропущена хоть одна


def today() -> str:
    return quiet.local_now().date().isoformat()


def record_missed(users: list[User]) -> None:
    """Эти бесплатные пользователи не получили заявку из-за дневного лимита (коммит — на вызывающем)."""
    day = today()
    for u in users:
        if u.missed_day != day:
            u.missed_day, u.missed_count = day, 0
        u.missed_count += 1


async def due_missed(s: AsyncSession) -> list[User]:
    """Кому пора сказать «лимит исчерпан» (раз в день)."""
    day, evening = today(), quiet.local_now().hour >= NOTE_EVENING_HOUR
    rows = (await s.scalars(select(User).where(
        User.missed_day == day, User.missed_count > 0, User.missed_noted != day,
        ~User.blocked, ~User.is_admin, User.plan == "free"))).all()
    return [u for u in rows if u.missed_count >= NOTE_AFTER or evening]


def missed_text(u: User, limit: int) -> str:
    return (f"⛔ Сегодняшний лимит бесплатного тарифа ({limit} заявок) исчерпан — ещё {u.missed_count} "
            "заявок по вашим нишам ушли другим специалистам, и новые продолжают приходить.\n"
            "💎 Полный тариф: все заявки сразу и без лимита. После оплаты пришлю заявки за последние сутки.")


async def week_stats(s: AsyncSession, user: User) -> tuple[int, int]:
    """(заявок за 7 дней по нишам и городу человека, из них пришло ему)."""
    week = now() - timedelta(days=7)
    roots = set((await s.scalars(select(UserCategory.category_id).where(UserCategory.user_id == user.tg_id))).all())
    if not roots:
        return 0, 0
    ids = roots | set((await s.scalars(select(Category.id).where(Category.parent_id.in_(roots)))).all())
    rows = (await s.execute(select(Lead.city, Lead.region).where(
        Lead.created_at > week, Lead.category_id.in_(ids), Lead.status != "spam"))).all()
    total = sum(geo.matches(user.city, user.only_city, c, r) for c, r in rows)
    got = await s.scalar(select(func.count()).select_from(Delivery).where(  # и ещё в пути (задержка бесплатного)
        Delivery.user_id == user.tg_id, Delivery.created_at > week)) or 0
    return total, got


async def start_trial(s: AsyncSession, user: User) -> int:
    """Пробный 💎 новому пользователю (один раз). Возвращает дни (0 — не дали). Коммит — на вызывающем."""
    days = await settings.get_int(s, "trial_days")
    row = await s.get(User, user.tg_id)
    if days <= 0 or row is None or row.trial_used or row.is_admin or row.plan == "full":
        return 0
    if await s.scalar(select(Payment.id).where(Payment.user_id == row.tg_id).limit(1)):
        return 0  # уже платил — пробный не нужен
    row.trial_used, row.on_trial = True, True
    plans.extend(row, timedelta(days=days))
    user.plan, user.plan_until, user.trial_used, user.on_trial = row.plan, row.plan_until, True, True
    return days


async def guarantee_due(s: AsyncSession) -> list[tuple[User, int, int]]:
    """Оплатившие, у кого за прошедшую неделю пришло меньше guarantee_min заявок: [(человек, пришло, +дней)].
    Дни уже добавлены (коммит — на вызывающем). Неделя считается от `guarantee_at`; первая — с первого обхода."""
    need, add = await settings.get_int(s, "guarantee_min"), await settings.get_int(s, "guarantee_days")
    out: list[tuple[User, int, int]] = []
    paid_ids = set((await s.scalars(select(Payment.user_id).where(Payment.days > 0, ~Payment.refunded))).all())
    users = (await s.scalars(select(User).where(User.plan == "full", ~User.is_admin, ~User.blocked))).all()
    for u in users:
        if u.plan_until is not None and u.plan_until <= now():
            continue
        if u.on_trial or u.tg_id not in paid_ids or u.paused:
            u.guarantee_at = None  # пробный, бонусный или на паузе — гарантия не считается
            continue
        if u.guarantee_at is None:
            u.guarantee_at = now()
            continue
        if now() - u.guarantee_at < timedelta(days=7):
            continue
        got = await s.scalar(select(func.count()).select_from(Delivery).where(
            Delivery.user_id == u.tg_id, Delivery.sent, Delivery.created_at > u.guarantee_at)) or 0
        u.guarantee_at = now()
        if need > 0 and add > 0 and got < need:
            plans.extend(u, timedelta(days=add))
            out.append((u, got, add))
    return out


def guarantee_text(got: int, add: int, need: int) -> str:
    return (f"🎁 За прошлую неделю пришло {got} заявок — меньше, чем мы обещаем ({need}). Добавили вам +{add} дн. 💎 бесплатно.\n"
            "Чтобы заявок было больше: 🎯 Мои ниши — добавьте соседние ниши, 📍 Мой город — выберите регион "
            "или «вся Россия».")
