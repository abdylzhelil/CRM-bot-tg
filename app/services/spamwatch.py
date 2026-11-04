"""🕵️ Сторож спама (владелец 29.10: «чтобы в спамные группы разведчик долго не оставался»).

Разведчик и так читает все сообщения своих групп. Здесь каждое сообщение от 5 слов бесплатно (правилами, без ИИ)
помечается «мусор» или «обычное»: реклама исполнителей, приглашения, вакансии/«заработок», серые темы, не по-русски.
Счётчики копятся в памяти и раз в ~10 минут переносятся в группу (`groups.watch_seen/watch_junk`).

Группа спамная, если прочитано ≥ MIN_SEEN сообщений, мусора ≥ JUNK_SHARE и заявок оттуда не было LEAD_DAYS дней —
тогда разведчик выходит (как «🧹 Проверка групп»: защищённые не покидаем, только «без заявок»).
Счётчики «скользят»: после WINDOW сообщений оба делятся пополам — группа, которая исправилась, перестаёт быть спамной.
"""
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Group, now

MIN_SEEN = 40      # меньше сообщений — рано судить
JUNK_SHARE = 0.7   # столько и больше мусора — спамная группа
LEAD_DAYS = 14     # если оттуда за столько дней была заявка — группу не трогаем
WINDOW = 300       # после стольких сообщений счётчики делятся пополам
DOUBT_SHARE = 0.45  # мусора столько и больше, но меньше JUNK_SHARE — «спорная»: пусть посмотрит ИИ (🧹 Проверка групп)
DOUBT_RECHECK = timedelta(days=3)

_counts: dict[int, list[int]] = {}  # tg_id группы → [прочитано, мусор]


def is_junk(text: str) -> bool | None:
    """True — мусор, False — обычное сообщение, None — слишком короткое, не считаем."""
    from app.services import antispam, groupcheck, junk, offers

    text = text or ""
    if len(text.split()) < 5:
        return None
    if not junk._cyrillic(text):
        return True
    if offers.rule_verdict(text) in ("offer", "other"):
        return True
    return bool(groupcheck._EARN.search(text) or antispam._GREY.search(text))


def note(group_tg_id: int, text: str) -> None:
    """Учесть сообщение из группы разведчика (вызывается на каждое сообщение, без базы)."""
    j = is_junk(text)
    if j is None:
        return
    c = _counts.setdefault(group_tg_id, [0, 0])
    c[0] += 1
    c[1] += int(j)


async def flush(s: AsyncSession) -> int:
    """Перенести накопленное в базу. Коммит — здесь. Возвращает число обновлённых групп."""
    if not _counts:
        return 0
    batch = dict(_counts)
    _counts.clear()
    groups = (await s.scalars(select(Group).where(Group.tg_id.in_(list(batch))))).all()
    for g in groups:
        seen, bad = batch[g.tg_id]
        g.watch_seen = (g.watch_seen or 0) + seen
        g.watch_junk = (g.watch_junk or 0) + bad
        if g.watch_seen > WINDOW:
            g.watch_seen //= 2
            g.watch_junk //= 2
    await s.commit()
    return len(groups)


def share(g: Group) -> float:
    return (g.watch_junk or 0) / g.watch_seen if g.watch_seen else 0.0


def verdict(g: Group) -> str:
    """Причина выйти ("" — группа в порядке или судить рано)."""
    if (g.watch_seen or 0) < MIN_SEEN or share(g) < JUNK_SHARE:
        return ""
    if g.last_lead_at is not None and g.last_lead_at > now() - timedelta(days=LEAD_DAYS):
        return ""
    return f"сторож: мусор {g.watch_junk} из {g.watch_seen} сообщений"[:120]


async def suspects(s: AsyncSession, limit: int = 0) -> list[Group]:
    """Вступившие группы разведчика, которые сторож считает спамными (сначала самые мусорные)."""
    rows = (await s.scalars(select(Group).where(
        Group.status == "joined", Group.kind == "group", Group.account_id.is_not(None), ~Group.no_leads,
        Group.watch_seen >= MIN_SEEN))).all()
    out = sorted((g for g in rows if verdict(g)), key=share, reverse=True)
    return out[:limit] if limit else out


async def doubtful(s: AsyncSession, limit: int = 2) -> list[int]:
    """Спорные группы (мусора от DOUBT_SHARE до JUNK_SHARE): их перепроверяет «🧹 Проверка групп» с ИИ —
    не ждать недели. Каждую не чаще раза в DOUBT_RECHECK."""
    rows = (await s.scalars(select(Group).where(
        Group.status == "joined", Group.kind == "group", Group.account_id.is_not(None), ~Group.no_leads,
        Group.watch_seen >= MIN_SEEN,
        Group.audit_at.is_(None) | (Group.audit_at < now() - DOUBT_RECHECK)))).all()
    out = sorted((g for g in rows if DOUBT_SHARE <= share(g) < JUNK_SHARE and not verdict(g)), key=share, reverse=True)
    return [g.id for g in out[:limit]]
