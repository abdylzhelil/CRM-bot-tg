"""🧹 Навести порядок в группах разведчика (владелец 25.10: «сократить число групп, убрать спамные и ненужные, но сначала
проверить места пользователей — эти группы должны остаться»).

Группа остаётся, если она защищена (каталог, реклама, просьба пользователя) или её место подходит списку мест
(`scout_places`; пустой список — места не проверяем) и она не спамная. Выходим только по кнопке владельца, с подтверждением;
вернуть группу можно: 🔍 Группы → 👋 Вышли → ↩️.
"""
import re
from datetime import timedelta

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AdPost, Group, Lead, User, now
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


# ---------- 🔄 Начать заново (владелец 29.10: «удалить все группы с базы и заново искать по правильным алгоритмам») ----------
_SPAM_REASON = re.compile(r"реклам|флуд|игры|заработ|серые|не на русском|спам|сторож|проверка:", re.I)


def reset_keep(g: Group) -> str:
    """Почему группу «начать заново» не трогает ("" — трогает)."""
    if g.kind != "group":
        return "канал"
    if g.bot_member:
        return "в ней наш бот"
    if g.status == "joined" and not g.account_id:
        return "вступил не разведчик"
    return groupcheck.protected(g)


def is_spam(g: Group) -> bool:
    """Группу запомнили как спамную — после сброса она остаётся в базе как «пропущена», чтобы не вступить в неё снова."""
    return bool(g.no_leads or g.audit_verdict in ("spam", "offtopic") or junk.title_reason(g.title)
                or _SPAM_REASON.search(g.skip_reason or ""))


async def reset_plan(s: AsyncSession) -> dict:
    """{'leave': [id вступивших — выйти], 'delete': n, 'spam': [id — запомнить], 'keep': n}"""
    with_posts = set((await s.scalars(select(AdPost.group_id).distinct())).all())
    out: dict = {"leave": [], "delete": 0, "spam": [], "keep": 0}
    for g in (await s.scalars(select(Group))).all():
        if reset_keep(g) or g.id in with_posts:
            out["keep"] += 1
            continue
        if g.status == "joined":
            out["leave"].append(g.id)
        if is_spam(g):
            out["spam"].append(g.id)
        else:
            out["delete"] += 1
    return out


async def reset_finish(s: AsyncSession, spam_ids: set[int]) -> dict[str, int]:
    """После выхода: удалить группы из базы (кроме защищённых, тех, откуда выйти не вышло, и спамных — их
    запоминаем как «пропущена»), сбросить очередь поисковых запросов. Заявки остаются (без ссылки на группу)."""
    with_posts = set((await s.scalars(select(AdPost.group_id).distinct())).all())
    gone, remembered, still = [], 0, 0
    for g in (await s.scalars(select(Group))).all():
        if reset_keep(g) or g.id in with_posts:
            continue
        if g.status == "joined":
            still += 1  # не получилось выйти (нет связи и т. п.) — остаётся, можно повторить
            continue
        if g.id in spam_ids or is_spam(g):
            g.status = "skipped"
            if not _SPAM_REASON.search(g.skip_reason or ""):
                g.skip_reason = "спам (запомнили при «начать заново»)"
            remembered += 1
            continue
        gone.append(g.id)
    for i in range(0, len(gone), 500):
        part = gone[i:i + 500]
        await s.execute(update(Lead).where(Lead.group_id.in_(part)).values(group_id=None))
        await s.execute(delete(Group).where(Group.id.in_(part)))
    for key in ("search_offset", "search_offset_home", "search_offset_near", "search_stage", "search_stage_found", "search_stage_wrapped"):
        await settings.put(s, key, "0")
    await settings.put(s, "scout_grow", "1")
    await s.commit()
    return {"deleted": len(gone), "remembered": remembered, "still": still}
