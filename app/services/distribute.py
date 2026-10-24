"""Раздача найденной заявки пользователям и доставка по расписанию.

Одну заявку получают не больше max_recipients пользователей (администраторы — всегда и сверх лимита),
так заказчика не заваливают сообщениями, а у исполнителя выше шанс. Платные — первыми и мгновенно,
бесплатные — с задержкой и дневным лимитом.

Заявки делятся поровну между пользователями (см. recipients): много заявок — у каждого свои, мало — одну получают
несколько, но по очереди с интервалом share_gap_min (выше уровень — раньше).
"""
import logging
import random
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Category, Delivery, Group, Lead, SpamVote, User, UserCategory, UserNegative, now
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


async def niche_flow(s: AsyncSession, root: int) -> int:
    """Сколько заявок по нише пришло за последние сутки (включая текущую)."""
    ids = (await s.scalars(select(Category.id).where((Category.id == root) | (Category.parent_id == root)))).all()
    return await s.scalar(select(func.count()).select_from(Lead).where(
        Lead.category_id.in_(ids), Lead.created_at > now() - timedelta(days=1))) or 1


async def recipients(s: AsyncSession, lead: Lead) -> list[tuple[User, str, int]]:
    """Кому отдать заявку: [(пользователь, когда, место в очереди)].

    когда: "now" — сразу, "late" — бесплатный тариф с задержкой. Если заявку получают несколько человек, они идут
    по очереди с интервалом share_gap_min: первым — автор группы, дальше по уровню (выше — раньше), при равных уровнях — у кого за сутки меньше заявок.

    Заявки делятся поровну: на каждую берём k человек с наименьшим числом заявок за сутки, где
    k = ceil(людей / заявок ниши за сутки). Заявок много — у каждого свои, разные; мало — одну получают несколько.
    Автор группы (из каталога) всегда получает заявки своей группы. Администраторы получают все.
    """
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
    group = await s.get(Group, lead.group_id) if lead.group_id else None
    finder_id = group.submitted_by if group is not None and group.catalog else 0
    wanted_id = group.wanted_by if group is not None else 0  # нашли группу по его запросу «🔎 Найти группы для меня»
    finder, wanted, admins, paid, free = None, None, [], [], []
    for u in users:
        if not await eligible(s, u, lead, voted, lead_lemmas):
            continue
        if u.tg_id == finder_id and not u.is_admin:
            finder = u
        elif u.tg_id == wanted_id and not u.is_admin:
            wanted = u
        else:
            (admins if u.is_admin else paid if is_paid(u) else free).append(u)

    limit = await settings.get_int(s, "free_daily_limit")
    load = {u.tg_id: await delivered_today(s, u.tg_id) for u in paid + free}
    first = []  # кто нашёл группу по своему запросу — первым в очереди, но в пределах своего тарифа
    if wanted is not None:
        if is_paid(wanted):
            first = [(wanted, "now")]
        elif await delivered_today(s, wanted.tg_id) < limit:
            first = [(wanted, "late")]
    from app.services import upsell

    upsell.record_missed([u for u in free if load[u.tg_id] >= limit])  # «лимит исчерпан — мимо прошло N»
    free = [u for u in free if load[u.tg_id] < limit]
    pool = len(paid) + len(free)
    k = min(await settings.get_int(s, "max_recipients"), -(-pool // await niche_flow(s, root)))
    # справедливо: меньше получивших сегодня — раньше; при равенстве — случайно
    order = lambda us: sorted(us, key=lambda u: (load[u.tg_id], random.random()))  # noqa: E731
    picked = [(u, "now") for u in order(paid)[:k]]
    picked += [(u, "late") for u in order(free)[: max(k - len(picked), 0)]]
    # очередь внутри заявки: автор группы первым, дальше — у кого уровень выше, при равенстве — у кого меньше заявок
    picked.sort(key=lambda p: (-p[0].level, load[p[0].tg_id]))
    chosen = [(u, "now", 0) for u in admins]
    queue = ([(finder, "now")] if finder is not None else []) + first + picked
    chosen += [(u, when, rank) for rank, (u, when) in enumerate(queue)]
    return chosen


async def distribute(s: AsyncSession, lead: Lead) -> list[Delivery]:
    delay = timedelta(minutes=await settings.get_int(s, "free_delay_min"))
    gap = timedelta(minutes=await settings.get_int(s, "share_gap_min"))
    rows = [Delivery(lead_id=lead.id, user_id=u.tg_id,
                     deliver_after=now() + (delay if when == "late" else timedelta()) + gap * rank)
            for u, when, rank in await recipients(s, lead)]
    s.add_all(rows)
    await s.commit()
    return rows


async def due(s: AsyncSession, limit: int = 50) -> list[tuple[Delivery, Lead, User]]:
    """Заявки, которые пора отправить (режим тишины пользователя учитывается здесь).

    Читаем порциями, пока не наберём limit: иначе ночью сотня отложенных «тихих» заявок заслонила бы остальных.
    На паузе — не шлём; старше суток (пауза, блокировка) — уже не шлём вовсе, чтобы потом не пришла пачка старья.
    """
    hour = quiet.local_now().hour
    fresh = now() - timedelta(days=1)
    out: list[tuple[Delivery, Lead, User]] = []
    last_id = 0
    while len(out) < limit:
        rows = (await s.execute(
            select(Delivery, Lead, User)
            .join(Lead, Lead.id == Delivery.lead_id)
            .join(User, User.tg_id == Delivery.user_id)
            .where(~Delivery.sent, Delivery.deliver_after <= now(), Delivery.deliver_after > fresh,
                   ~User.blocked, ~User.paused, Delivery.id > last_id)
            .order_by(Delivery.id)
            .limit(limit * 4)
        )).all()
        if not rows:
            break
        last_id = rows[-1][0].id
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
