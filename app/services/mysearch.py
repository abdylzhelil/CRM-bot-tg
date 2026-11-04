"""«🔎 Найти группы для меня»: человек пишет слова («ремонт Подольск»), наш разведчик ищет по ним группы,
проверяет их (junk) и вступает. Заявки из найденных групп этот человек получает первым (distribute.recipients).

Чужие аккаунты не нужны: ищет и читает наш разведчик. Число запросов ограничено тарифом.
"""
import re
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Group, User, UserSearch, now
from app.services import settings

PER_RUN = 5  # запросов пользователей за один заход разведчика (чтобы не упереться в лимиты Telegram)
STATUS_WORDS = {"joined": "читаем", "approved": "в очереди", "candidate": "в очереди", "requested": "ждём одобрения",
                "skipped": "не подошли", "covered": "там уже наш бот", "left": "вышли (не было заявок)"}


class SearchError(ValueError):
    pass


def is_paid(user: User) -> bool:
    from app.services.distribute import is_paid as paid

    return paid(user)


async def limit_for(s: AsyncSession, user: User) -> int:
    if user.is_admin:
        return 50
    return await settings.get_int(s, "user_search_full" if is_paid(user) else "user_search_free")


async def my(s: AsyncSession, user_id: int) -> list[UserSearch]:
    return list((await s.scalars(select(UserSearch).where(UserSearch.user_id == user_id)
                                 .order_by(UserSearch.id))).all())


def clean(text: str) -> str:
    text = re.sub(r"\s+", " ", (text or "").strip().strip("«»\"'"))
    if len(text) > 64:
        raise SearchError("Слишком длинно — до 64 знаков. Например: «ремонт квартир Подольск».")
    if len(re.findall(r"[^\W\d_]", text)) < 3:
        raise SearchError("Напишите словами, что искать. Например: «свадьбы Казань» или «ремонт Подольск».")
    return text


async def add(s: AsyncSession, user: User, text: str) -> UserSearch:
    """Новый запрос. Коммит — на вызывающем."""
    text = clean(text)
    mine = await my(s, user.tg_id)
    if any(q.text.casefold() == text.casefold() for q in mine):
        raise SearchError("Такой запрос у вас уже есть.")
    limit = await limit_for(s, user)
    if len(mine) >= limit:
        if not limit:
            raise SearchError("На бесплатном тарифе эта функция выключена — она доступна с 💎.")
        tail = "" if is_paid(user) or user.is_admin else " на бесплатном тарифе (с 💎 больше)"
        raise SearchError(f"Можно до {limit} запросов{tail}. Удалите ненужный, чтобы добавить новый.")
    q = UserSearch(user_id=user.tg_id, text=text)
    s.add(q)
    await s.flush()
    return q


async def remove(s: AsyncSession, user_id: int, sid: int, admin: bool = False) -> bool:
    """Удалить запрос. Уже найденные группы остаются (разведчик их читает), но без приоритета для человека."""
    q = await s.get(UserSearch, sid)
    if q is None or (q.user_id != user_id and not admin):
        return False
    for g in (await s.scalars(select(Group).where(Group.wanted_search == sid))).all():
        g.wanted_by, g.wanted_search = 0, 0
    await s.delete(q)
    return True


async def counts(s: AsyncSession, sid: int) -> dict[str, int]:
    rows = (await s.execute(select(Group.status, func.count()).where(Group.wanted_search == sid)
                            .group_by(Group.status))).all()
    return {st: n for st, n in rows}


def describe(q: UserSearch, c: dict[str, int]) -> str:
    if q.last_run_at is None:
        return "⏳ ещё не искали — разведчик возьмёт запрос в ближайшие минуты"
    parts = [f"{STATUS_WORDS.get(st, st)} {n}" for st, n in sorted(c.items(), key=lambda x: -x[1]) if n]
    return (f"найдено групп: {q.found}" + (f" ({', '.join(parts)})" if parts else "")
            + f", последний поиск {q.last_run_at:%d.%m}")


async def due(s: AsyncSession, limit: int = PER_RUN) -> list[UserSearch]:
    """Новые запросы первыми, потом те, что давно не повторяли."""
    days = await settings.get_int(s, "user_search_days")
    cutoff = now() - timedelta(days=days)
    rows = (await s.scalars(select(UserSearch).where(
        (UserSearch.last_run_at.is_(None)) | (UserSearch.last_run_at < cutoff))
        .order_by(UserSearch.last_run_at.is_not(None), UserSearch.last_run_at, UserSearch.id).limit(limit))).all()
    return list(rows)


async def has_new(s: AsyncSession) -> bool:
    return bool(await s.scalar(select(UserSearch.id).where(UserSearch.last_run_at.is_(None)).limit(1)))
