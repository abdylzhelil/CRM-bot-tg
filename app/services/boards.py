"""📊 Рейтинг заявок по нишам: в закрепе групп с ботом КлиентРадар и в самом боте.

В группе: бот держит одно сообщение (редактирует, новое не шлёт) — сколько заявок по нишам в городе группы
за сутки и неделю + кнопка «📥 Получать заявки» (ссылка t.me/бот?start=src_g<id>). Пишет только там, где он
админ с правом закреплять: значит, админ группы сам этого захотел. Без прав — молчит, как раньше.

Кто пришёл по кнопке (users.source = "g<id>") и оплатил 💎, — админ, добавивший бота (groups.board_admin),
получает `board_percent`% купленных дней (решение владельца 24.10: комиссия днями, не деньгами).

В боте: те же цифры для города пользователя; список самих заявок по нише — только на 💎.
Авторов заявок нигде не показываем.
"""
import re
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Category, Group, Lead, User, now
from app.services import geo, plans, settings

TOP = 10
_SOURCE = re.compile(r"^g(\d+)$")


def source_tag(group_id: int) -> str:
    return f"g{group_id}"


def join_link(bot_username: str, group_id: int) -> str:
    return f"https://t.me/{bot_username}?start=src_{source_tag(group_id)}"


def place_of(g: Group | None) -> tuple[str, str, str]:
    """(город, регион, подпись) группы — заявки какого места показывать."""
    if g is None:
        return "", "", "вся Россия"
    city = g.city or ""
    region = g.region or geo.region_of(city)
    if city:
        return city, region, city
    if region:
        return "", region, region
    return "", "", "вся Россия"


def _where(city: str, region: str):
    conds = [Lead.status != "spam"]
    if city:
        conds.append((Lead.city == city) | ((Lead.city == "") & (Lead.region == region)) if region else Lead.city == city)
    elif region:
        conds.append(Lead.region == region)
    return conds


async def counts(s: AsyncSession, city: str = "", region: str = "") -> tuple[list[tuple[int, str, int, int]], int, int]:
    """([(id ниши, название, за неделю, за сутки)], всего за сутки, всего за неделю). Ниши — верхнего уровня."""
    day, week = now() - timedelta(days=1), now() - timedelta(days=7)
    rows = (await s.execute(select(Lead.category_id, Lead.created_at).where(
        Lead.created_at > week, Lead.category_id.is_not(None), *_where(city, region)))).all()
    cats = {c.id: c for c in (await s.scalars(select(Category))).all()}
    agg: dict[int, list[int]] = {}
    for cat_id, at in rows:
        c = cats.get(cat_id)
        while c is not None and c.parent_id and c.parent_id in cats:  # ниша верхнего уровня
            c = cats[c.parent_id]
        if c is None:
            continue
        a = agg.setdefault(c.id, [0, 0])
        a[0] += 1
        a[1] += at > day
    items = sorted(((cid, f"{cats[cid].emoji} {cats[cid].name}".strip(), w, d) for cid, (w, d) in agg.items()),
                   key=lambda x: (-x[2], x[1]))
    return items, sum(x[3] for x in items), sum(x[2] for x in items)


async def text(s: AsyncSession, city: str, region: str, label: str, footer: str = "") -> str:
    items, day, week = await counts(s, city, region)
    lines = [f"📊 <b>Заявки на услуги — {label}</b>", f"За сутки: {day} · за неделю: {week}", ""]
    if items:
        for i, (_, name, w, d) in enumerate(items[:TOP], 1):
            lines.append(f"{i}. {name} — {w}" + (f" (+{d} за сутки)" if d else ""))
    else:
        lines.append("Пока тихо — заявки появятся здесь, как только их найдём.")
    if footer:
        lines += ["", footer]
    return "\n".join(lines)


async def group_text(s: AsyncSession, g: Group) -> str:
    city, region, label = place_of(g)
    return await text(s, city, region, label,
                      "Заказчики ищут исполнителей прямо сейчас. Получайте такие заявки себе в личные сообщения 👇\n"
                      f"<i>обновлено {(now() + timedelta(hours=3)):%d.%m %H:%M} МСК</i>")


async def due(s: AsyncSession) -> list[Group]:
    """Группы, где бот есть и рейтинг пора обновить."""
    if await settings.get(s, "board_enabled") != "1":
        return []
    every = timedelta(hours=await settings.get_int(s, "board_hours"))
    return list((await s.scalars(select(Group).where(
        Group.bot_member, Group.board_on, Group.status == "joined", Group.kind == "group",
        Group.board_at.is_(None) | (Group.board_at < now() - every)).limit(10))).all())


async def reward(s: AsyncSession, user: User, bought: timedelta) -> tuple[int, timedelta, str] | None:
    """Покупатель пришёл из группы с ботом — админу группы board_percent% купленных дней. Коммит — на вызывающем."""
    m = _SOURCE.match(user.source or "")
    if not m:
        return None
    g = await s.get(Group, int(m.group(1)))
    if g is None or not g.board_admin or g.board_admin == user.tg_id:
        return None
    admin = await s.get(User, g.board_admin)
    percent = await settings.get_int(s, "board_percent")
    share = bought * percent / 100
    if admin is None or share <= timedelta(0):
        return None
    plans.extend(admin, share)
    g.board_earned = (g.board_earned or 0) + share.total_seconds() / 86400
    return admin.tg_id, share, g.title


async def my_groups(s: AsyncSession, tg_id: int) -> list[tuple[Group, int, int]]:
    """Группы, куда этот человек добавил бота: (группа, пришло людей, из них с 💎)."""
    out = []
    for g in (await s.scalars(select(Group).where(Group.board_admin == tg_id))).all():
        tag = source_tag(g.id)
        came = await s.scalar(select(func.count()).select_from(User).where(User.source == tag)) or 0
        paid = await s.scalar(select(func.count()).select_from(User).where(User.source == tag, User.plan == "full")) or 0
        out.append((g, came, paid))
    return out


async def leads_of(s: AsyncSession, cat_id: int, city: str, region: str, limit: int = 10) -> list[Lead]:
    """Последние заявки ниши (и её подниш) в месте — только для 💎."""
    ids = {cat_id} | set((await s.scalars(select(Category.id).where(Category.parent_id == cat_id))).all())
    return list((await s.scalars(select(Lead).where(
        Lead.category_id.in_(ids), Lead.created_at > now() - timedelta(days=7), Lead.gone_at.is_(None),
        *_where(city, region))
        .order_by(Lead.created_at.desc()).limit(limit))).all())
