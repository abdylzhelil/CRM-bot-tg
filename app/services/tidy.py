"""🧹 Навести порядок в группах разведчика (владелец 25.10: «сократить число групп, убрать спамные и ненужные, но сначала
проверить места пользователей — эти группы должны остаться»).

Группа остаётся, если она защищена (каталог, реклама, просьба пользователя) или её место подходит списку мест
(`scout_places`; пустой список — места не проверяем) и она не спамная. Выходим только по кнопке владельца, с подтверждением;
вернуть группу можно: 🔍 Группы → 👋 Вышли → ↩️.
"""
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Group, User, now
from app.services import geo, groupcheck, junk, settings

QUIET_DAYS = 14  # «без заявок»: состоит дольше стольки дней и заявок оттуда не было ни одной

KINDS = {
    "spam": ("🗑 Спамные и рекламные", "реклама, флуд, «заработок» или сама группа помечена «без заявок»"),
    "place": ("🌍 Чужие места", "место группы известно и не входит в выбранные места (в т. ч. заграница)"),
    "quiet": ("😴 Без заявок", f"состоит больше {QUIET_DAYS} дней, заявок оттуда ни одной"),
}


async def user_places(s: AsyncSession) -> list[tuple[str, int]]:
    """Места, которые отметили пользователи: [(город или регион, сколько людей)]. Город даёт и его регион;
    «вся Россия» (пусто) не считаем — это не ограничение."""
    rows = (await s.execute(select(User.city, func.count()).where(User.onboarded, ~User.blocked, User.city != "")
                            .group_by(User.city))).all()
    agg: dict[str, int] = {}
    for city, n in rows:
        found_city, found_region = geo.find(city)
        place = city if city in geo.KNOWN_PLACES else (found_city or found_region)
        if not place:
            continue
        agg[place] = agg.get(place, 0) + n
        region = geo.region_of(place)
        if region and region != place:
            agg[region] = agg.get(region, 0) + n
    return sorted(agg.items(), key=lambda x: -x[1])


def allowed_default(users: list[tuple[str, int]], home: str) -> list[str]:
    out = [p for p, _ in users]
    if home and home not in out:
        out.append(home)
    return out


async def classify(s: AsyncSession) -> dict:
    """{'spam': [Group], 'place': [...], 'quiet': [...], 'keep': n, 'protected': n, 'allowed': [...]}"""
    allowed = geo.parse_places(await settings.get(s, "scout_places"))
    groups = (await s.scalars(select(Group).where(Group.status == "joined", Group.kind == "group"))).all()
    out: dict = {"spam": [], "place": [], "quiet": [], "keep": 0, "protected": 0, "allowed": allowed, "total": len(groups)}
    old = now() - timedelta(days=QUIET_DAYS)
    for g in groups:
        if groupcheck.protected(g):
            out["protected"] += 1
            continue
        if g.no_leads or g.audit_verdict in ("spam", "offtopic") or junk.title_reason(g.title):
            out["spam"].append(g)
        elif allowed and geo.place_state(g.city, g.region, g.title, allowed) is False:
            out["place"].append(g)
        elif (g.leads_count or 0) == 0 and g.joined_at is not None and g.joined_at < old:
            out["quiet"].append(g)
        else:
            out["keep"] += 1
    return out
