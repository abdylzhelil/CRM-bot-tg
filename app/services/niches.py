"""Новые ниши от пользователей: разбор текста, создание на проверку, одобрение."""
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Category, Keyword, User, UserCategory, now
from app.services import rules, settings
from app.services.distribute import day_start

MIN_WORDS, MAX_WORDS, MAX_NAME = 3, 30, 40


class NicheError(Exception):
    """Понятная пользователю причина отказа."""


def parse(text: str) -> tuple[str, list[str]]:
    """«Ремонт квартир: ремонт, отделка, плитка» или название в первой строке, слова — ниже."""
    first, _, rest = text.strip().partition("\n")
    if ":" in first:
        name, _, tail = first.partition(":")
        rest = tail + "\n" + rest
    else:
        name = first
    words = []
    for w in rest.replace("\n", ",").split(","):
        w = w.strip().lower()
        if w and w not in words:
            words.append(w[:60])
    return name.strip()[:MAX_NAME], words[:MAX_WORDS]


async def propose(s: AsyncSession, user: User, text: str) -> Category:
    name, words = parse(text)
    if len(name) < 3:
        raise NicheError("Название слишком короткое.")
    if len(words) < MIN_WORDS:
        raise NicheError(f"Нужно хотя бы {MIN_WORDS} слова, по которым искать заявки (через запятую).")
    # регистр сравниваем в Python: lower() в SQLite не работает с кириллицей
    names = {n.casefold() for n in (await s.scalars(select(Category.name).where(Category.parent_id.is_(None)))).all()}
    if name.casefold() in names:
        raise NicheError("Такая ниша уже есть — найдите её в списке.")
    if not user.is_admin:
        today = await s.scalar(select(func.count()).select_from(Category).where(
            Category.proposed_by == user.tg_id, Category.proposed_at >= day_start())) or 0
        if today >= await settings.get_int(s, "niche_daily_limit"):
            raise NicheError("На сегодня лимит предложений исчерпан.")
    cat = Category(name=name, emoji="📌", proposed_by=user.tg_id, proposed_at=now(), pending=not user.is_admin,
                   enabled=user.is_admin)
    s.add(cat)
    await s.flush()
    s.add_all(Keyword(kind="category", word=w, category_id=cat.id) for w in words)
    if user.is_admin:
        s.add(UserCategory(user_id=user.tg_id, category_id=cat.id))
    await s.commit()
    rules.invalidate()
    return cat


async def approve(s: AsyncSession, cat: Category) -> None:
    cat.pending, cat.enabled = False, True
    if cat.proposed_by and await s.get(UserCategory, (cat.proposed_by, cat.id)) is None:
        s.add(UserCategory(user_id=cat.proposed_by, category_id=cat.id))  # автору ниша подключается сразу
    await s.commit()
    rules.invalidate()


async def reject(s: AsyncSession, cat: Category) -> None:
    await s.execute(delete(Keyword).where(Keyword.category_id == cat.id))
    await s.delete(cat)
    await s.commit()
    rules.invalidate()
