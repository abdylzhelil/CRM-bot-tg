"""Уровни пользователей, чьи группы приносят заявки, и рейтинг по городам."""
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Group, User
from app.services import geo, settings

# (номер, название, нужно заявок из групп пользователя)
LEVELS = [
    (0, "🌱 Новичок", None),
    (1, "🔎 Разведчик", 0),  # есть хотя бы одна группа в каталоге
    (2, "🥉 Бронза", 10),
    (3, "🥈 Серебро", 50),
    (4, "🥇 Золото", 200),
    (5, "👑 Легенда", 1000),
]


def level_of(groups: int, leads: int) -> int:
    level = 0
    for num, _, need in LEVELS:
        if need is not None and groups > 0 and leads >= need:
            level = num
    return level


def name(level: int) -> str:
    return LEVELS[level][1]


def next_step(level: int, leads: int) -> str:
    if level >= LEVELS[-1][0]:
        return "Вы на высшем уровне 👑"
    if level == 0:
        return "Добавьте группу в 🌐 Каталог — станете 🔎 Разведчиком"
    _, nxt, need = LEVELS[level + 1]
    return f"До уровня {nxt}: ещё {max(need - leads, 0)} заявок из ваших групп"


async def stats(s: AsyncSession, user_id: int) -> tuple[int, int]:
    """(групп в каталоге, заявок из них)."""
    row = (await s.execute(select(func.count(), func.coalesce(func.sum(Group.leads_count), 0)).where(
        Group.submitted_by == user_id, Group.catalog))).one()
    return int(row[0]), int(row[1])


async def current(s: AsyncSession, user_id: int) -> int:
    groups, leads = await stats(s, user_id)
    return level_of(groups, leads)


async def refresh(s: AsyncSession, user_id: int) -> int | None:
    """Пересчитать уровень; вернуть новый, если он вырос."""
    u = await s.get(User, user_id)
    if u is None or u.is_admin:
        return None
    level = await current(s, user_id)
    if level > u.level:
        u.level = level
        await s.commit()
        return level
    return None


async def rating(s: AsyncSession, city: str = "", limit: int = 10) -> list[tuple[User, int]]:
    """Лучшие авторы групп: по заявкам из их групп; city — только группы этого города/региона."""
    groups = (await s.scalars(select(Group).where(Group.catalog, Group.submitted_by != 0))).all()
    score: dict[int, int] = {}
    for g in groups:
        if city and g.city and not geo.matches(city, False, g.city, geo.region_of(g.city)):
            continue
        if city and not g.city:
            continue
        score[g.submitted_by] = score.get(g.submitted_by, 0) + g.leads_count
    out = []
    for uid, pts in sorted(score.items(), key=lambda kv: -kv[1]):
        u = await s.get(User, uid)
        if u is not None and not u.is_admin:
            out.append((u, pts))
    return out[:limit] if limit else out
