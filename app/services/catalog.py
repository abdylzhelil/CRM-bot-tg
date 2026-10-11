"""Каталог групп от пользователей: разбор ссылки, проверка, одобрение, бонусы за заявки.

Только открытые группы и каналы (есть публичный @username). Закрытые ссылки-приглашения не принимаются.
"""
import re
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import FeedChat, Group, User, now
from app.services import geo, settings
from app.services.distribute import day_start, is_paid, root_category

_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")
_PREFIX = re.compile(r"^(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/", re.I)


def parse_username(raw: str) -> str | None:
    """«https://t.me/name», «t.me/s/name/12», «@name», «name» -> name; приглашения (+…, joinchat, c/) — None."""
    text = raw.strip().split()[0] if raw.strip() else ""
    if _PREFIX.match(text):
        parts = [p for p in _PREFIX.sub("", text).split("?")[0].split("/") if p]
        if parts and parts[0] == "s":
            parts = parts[1:]
        if not parts or parts[0] in ("joinchat", "c") or parts[0].startswith("+"):
            return None
        name = parts[0]
    else:
        name = text.lstrip("@")
    return name if _NAME.match(name) else None


@dataclass
class ChatInfo:
    tg_id: int
    username: str
    title: str
    kind: str  # group | channel
    members: int


class SubmitError(Exception):
    """Понятная пользователю причина отказа."""


async def lookup(bot, username: str) -> ChatInfo:
    """Данные открытой группы/канала через Bot API (бот в группе состоять не обязан)."""
    try:
        chat = await bot.get_chat(f"@{username}")
    except Exception:
        raise SubmitError("Не нашёл такую открытую группу или канал. Проверьте ссылку.") from None
    if chat.type not in ("group", "supergroup", "channel"):
        raise SubmitError("Это не группа и не канал. Принимаем только открытые группы и каналы.")
    try:
        members = await bot.get_chat_member_count(chat.id)
    except Exception:
        members = 0
    return ChatInfo(
        tg_id=chat.id, username=chat.username or username, title=(chat.title or username)[:255],
        kind="channel" if chat.type == "channel" else "group", members=members,
    )


async def start_submission(s: AsyncSession, user: User, info: ChatInfo) -> Group:
    """Создаёт черновик (или продолжает свой). Дальше пользователь выбирает категорию и город."""
    if not user.is_admin:
        min_members = await settings.get_int(s, "catalog_min_members")
        if info.members < min_members:
            raise SubmitError(f"Слишком маленькая группа: {info.members} уч., нужно от {min_members}.")
        today = await s.scalar(select(func.count()).select_from(Group).where(
            Group.submitted_by == user.tg_id, Group.submitted_at >= day_start(),
            Group.status.not_in(("draft", "rejected")))) or 0
        if today >= await settings.get_int(s, "submit_daily_limit"):
            raise SubmitError("На сегодня лимит добавленных групп исчерпан. Приходите завтра.")
    g = await s.scalar(select(Group).where(Group.tg_id == info.tg_id))
    if g is not None:
        mine_draft = g.submitted_by == user.tg_id and g.status == "draft"
        free_to_claim = g.submitted_by == 0 and g.status in ("candidate", "skipped")
        if not (mine_draft or free_to_claim):
            raise SubmitError("Эта группа уже есть в нашей базе 👍")
    else:
        g = Group(tg_id=info.tg_id)
        s.add(g)
    g.username, g.title, g.members, g.kind = info.username, info.title, info.members, info.kind
    g.status, g.submitted_by = "draft", user.tg_id
    await s.commit()
    return g


async def finish_submission(s: AsyncSession, g: Group) -> None:
    g.status, g.submitted_at = "pending", now()
    await s.commit()


async def approve(s: AsyncSession, g: Group) -> None:
    """Одобрено админом: в каталог; группу читает разведчик, канал — опрос t.me/s/."""
    g.catalog = True
    if g.kind == "channel":
        g.status = "joined"
    elif await s.get(FeedChat, g.tg_id) is not None:
        g.status = "covered"  # там уже наш бот модерации
    else:
        g.status = "approved"
    await s.commit()


async def reject(s: AsyncSession, g: Group) -> None:
    g.status, g.catalog = "rejected", False
    await s.commit()


async def pending_count(s: AsyncSession) -> int:
    return await s.scalar(select(func.count()).select_from(Group).where(Group.status == "pending")) or 0


async def browse(s: AsyncSession, user: User, roots: set[int], everything: bool, limit: int = 12) -> list[Group]:
    """Каталог: группы моих ниш и моего города (everything — без фильтров)."""
    groups = (await s.scalars(select(Group).where(Group.catalog).order_by(Group.leads_count.desc(), Group.members.desc()))).all()
    if everything:
        return list(groups[:limit])
    out = []
    for g in groups:
        if roots and await root_category(s, g.category_id) not in roots:
            continue
        if user.city and g.city and not geo.matches(user.city, False, g.city, geo.region_of(g.city)):
            continue
        out.append(g)
        if len(out) >= limit:
            break
    return out


def bonus_days(g: Group, per: int) -> int:
    return g.bonus_rewarded // per if per > 0 else 0


async def award_bonus(s: AsyncSession, group_id: int | None) -> tuple[int, int] | None:
    """За каждые N заявок из группы пользователя — +1 день полного тарифа ему.

    Возвращает (id пользователя, дней) если бонус выдан.
    """
    if not group_id:
        return None
    g = await s.get(Group, group_id)
    if g is None or not g.submitted_by or not g.catalog:
        return None
    per = await settings.get_int(s, "bonus_leads")
    days = (g.leads_count - g.bonus_rewarded) // per if per > 0 else 0
    if days <= 0:
        return None
    g.bonus_rewarded += days * per
    u = await s.get(User, g.submitted_by)
    forever = u is not None and u.plan == "full" and u.plan_until is None
    if u is None or u.is_admin or forever:
        await s.commit()
        return None
    start = u.plan_until if is_paid(u) else now()
    u.plan, u.plan_until = "full", start + timedelta(days=days)
    await s.commit()
    return g.submitted_by, days
