"""Раздача найденной заявки пользователям и доставка по расписанию.

Одну заявку получают не больше max_recipients пользователей (администраторы — всегда и сверх лимита),
так заказчика не заваливают сообщениями, а у исполнителя выше шанс. Платные — первыми и мгновенно,
бесплатные — с задержкой и дневным лимитом.
"""
import logging
import random
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Category, Delivery, Lead, SpamVote, User, UserCategory, UserNegative, now
from app.services import geo, quiet, settings
from app.services.text import compile_negatives, lemmas, negative_hit, tokens

log = logging.getLogger(__name__)


def is_paid(user: User) -> bool:
    return user.plan == "full" and (user.plan_until is None or user.plan_until > now())


def day_start():
    """Начало текущих суток по местному времени, в UTC-формате базы."""
    local = quiet.local_now()
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return (now() - (local - midnight)).replace(microsecond=0)


async def root_category(s: AsyncSession, cat_id: int | None) -> int | None:
    cat = await s.get(Category, cat_id) if cat_id else None
    while cat is not None and cat.parent_id:
        cat = await s.get(Category, cat.parent_id)
    return cat.id if cat else None


async def delivered_today(s: AsyncSession, user_id: int) -> int:
    return await s.scalar(
        select(func.count()).select_from(Delivery).where(Delivery.user_id == user_id, Delivery.created_at >= day_start())
    ) or 0


async def eligible(s: AsyncSession, u: User, lead: Lead, voted: set[int], lead_lemmas: list[str]) -> bool:
    """Подходит ли заявка пользователю (ниша проверяется отдельно)."""
    if u.tg_id in voted or u.tg_id == lead.author_id:
        return False
    if not u.is_admin and not geo.matches(u.city, u.only_city, lead.city, lead.region):
        return False
    negatives = (await s.scalars(select(UserNegative.word).where(UserNegative.user_id == u.tg_id))).all()
    raw = tokens(lead.text)
    return not negative_hit(lead_lemmas, raw, compile_negatives(list(negatives)))


async def recipients(s: AsyncSession, lead: Lead) -> list[tuple[User, bool]]:
    """Кому отдать заявку: [(пользователь, мгновенно ли)]."""
    root = await root_category(s, lead.category_id)
    if root is None:
        return []
    users = (await s.scalars(
        select(User).join(UserCategory, UserCategory.user_id == User.tg_id).where(
            UserCategory.category_id == root, ~User.blocked, ~User.paused, User.onboarded,
        )
    )).all()
    voted = set((await s.scalars(select(SpamVote.user_id).where(SpamVote.author_id == lead.author_id))).all())
    lead_lemmas = lemmas(lead.text)
    admins, paid, free = [], [], []
    for u in users:
        if await eligible(s, u, lead, voted, lead_lemmas):
            (admins if u.is_admin else paid if is_paid(u) else free).append(u)

    cap = await settings.get_int(s, "max_recipients")
    limit = await settings.get_int(s, "free_daily_limit")
    load = {u.tg_id: await delivered_today(s, u.tg_id) for u in paid + free}
    # справедливо: меньше получивших сегодня — раньше; при равенстве — случайно
    order = lambda us: sorted(us, key=lambda u: (load[u.tg_id], random.random()))  # noqa: E731
    chosen = [(u, True) for u in admins]
    for u in order(paid):
        if cap <= 0:
            break
        chosen.append((u, True))
        cap -= 1
    for u in order(free):
        if cap <= 0:
            break
        if load[u.tg_id] >= limit:
            continue
        chosen.append((u, False))
        cap -= 1
    return chosen


async def distribute(s: AsyncSession, lead: Lead) -> list[Delivery]:
    delay = timedelta(minutes=await settings.get_int(s, "free_delay_min"))
    rows = [
        Delivery(lead_id=lead.id, user_id=u.tg_id, deliver_after=now() if instant else now() + delay)
        for u, instant in await recipients(s, lead)
    ]
    s.add_all(rows)
    await s.commit()
    return rows


async def due(s: AsyncSession, limit: int = 50) -> list[tuple[Delivery, Lead, User]]:
    """Заявки, которые пора отправить (режим тишины пользователя учитывается здесь)."""
    rows = (await s.execute(
        select(Delivery, Lead, User)
        .join(Lead, Lead.id == Delivery.lead_id)
        .join(User, User.tg_id == Delivery.user_id)
        .where(~Delivery.sent, Delivery.deliver_after <= now(), ~User.blocked)
        .order_by(Delivery.id)
        .limit(limit * 4)
    )).all()
    hour = quiet.local_now().hour
    out = []
    for d, lead, user in rows:
        if quiet.in_quiet(hour, quiet.parse_hours(user.quiet_hours)) and not quiet.is_hot(lead.text):
            continue  # придёт, когда закончится тишина
        out.append((d, lead, user))
        if len(out) >= limit:
            break
    return out


async def preview(s: AsyncSession, user: User, limit: int = 5) -> list[Delivery]:
    """Заявки за последние сутки для нового пользователя — чтобы сразу увидеть пользу.

    Такие показы не тратят дневной лимит бесплатного тарифа (created_at — до начала суток).
    """
    roots = set((await s.scalars(select(UserCategory.category_id).where(UserCategory.user_id == user.tg_id))).all())
    leads = (await s.scalars(
        select(Lead).where(Lead.created_at > now() - timedelta(days=1)).order_by(Lead.id.desc()).limit(300)
    )).all()
    have = set((await s.scalars(select(Delivery.lead_id).where(Delivery.user_id == user.tg_id))).all())
    out: list[Delivery] = []
    for lead in leads:
        if lead.id in have or lead.suspicious or await root_category(s, lead.category_id) not in roots:
            continue
        if not await eligible(s, user, lead, set(), lemmas(lead.text)):
            continue
        out.append(Delivery(lead_id=lead.id, user_id=user.tg_id, sent=True, deliver_after=now(),
                            created_at=day_start() - timedelta(seconds=1)))
        if len(out) >= limit:
            break
    s.add_all(out)
    await s.commit()
    return out
