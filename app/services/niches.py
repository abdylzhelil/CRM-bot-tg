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


async def detach(s: AsyncSession, ids: list[int]) -> None:
    """Перед удалением ниш: отвязать от них группы, заявки и клиентов (они остаются, просто без ниши),
    убрать слова и выбор пользователей. Иначе PostgreSQL не даст удалить (внешние ключи)."""
    from sqlalchemy import update

    from app.models import Client, Group, Lead

    for model in (Group, Lead, Client):
        await s.execute(update(model).where(model.category_id.in_(ids)).values(category_id=None))
    await s.execute(delete(UserCategory).where(UserCategory.category_id.in_(ids)))
    await s.execute(delete(Keyword).where(Keyword.category_id.in_(ids)))


async def reject(s: AsyncSession, cat: Category) -> None:
    await detach(s, [cat.id])
    await s.delete(cat)
    await s.commit()
    rules.invalidate()


# ---------- разделы, поиск, похожие ниши ----------

def section_order() -> list[str]:
    from app.seed import SECTIONS

    return [title for title, _ in SECTIONS]


async def visible_roots(s: AsyncSession) -> list[Category]:
    return list((await s.scalars(select(Category).where(
        Category.parent_id.is_(None), Category.enabled, ~Category.pending).order_by(Category.id))).all())


async def grouped(s: AsyncSession) -> list[tuple[str, list[Category]]]:
    """[(раздел, ниши)] в порядке seed.SECTIONS, потом прочие разделы, «📌 Другое» — в конце. Пустые не показываем."""
    from app.seed import OTHER

    by: dict[str, list[Category]] = {}
    for c in await visible_roots(s):
        by.setdefault(c.section or OTHER, []).append(c)
    order = section_order()
    keys = sorted(by, key=lambda k: (k == OTHER, order.index(k) if k in order else len(order), k))
    return [(k, by[k]) for k in keys]


async def all_sections(s: AsyncSession) -> list[str]:
    """Разделы для выбора админом: стандартные + те, что уже есть в базе, + «Другое»."""
    from app.seed import OTHER

    have = set((await s.scalars(select(Category.section).where(Category.parent_id.is_(None)))).all())
    out = section_order()
    out += sorted(x for x in have if x and x not in out and x != OTHER)
    return out + [OTHER]


_STOP = {"для", "на", "по", "в", "во", "с", "со", "и", "или", "к", "от", "до", "под", "за", "из", "у", "о", "об", "не", "мой",
         "я", "мы", "нужный", "нужно", "искать", "кто", "как", "что", "это", "свой", "заказ", "услуга", "работа", "ремонт"}


def _meaningful(words) -> set[str]:
    """Без предлогов и общих слов («ремонт» есть в пяти нишах — по нему не различить)."""
    return {w for w in words if len(w) >= 2 and w not in _STOP}


async def find(s: AsyncSession, query: str, limit: int = 8) -> list[Category]:
    """Ниши по слову: «сантехник», «торт», «вайлдберриз». Сравниваем основы слов с названием, подразделами и словами ниши."""
    from app.models import Keyword
    from app.services.text import lemmas

    q = _meaningful(lemmas(query))
    if not q:
        return []
    roots = await visible_roots(s)
    ids = {c.id for c in roots}
    extra: dict[int, list[str]] = {c.id: [c.name] for c in roots}
    for parent_id, name in (await s.execute(select(Category.parent_id, Category.name).where(
            Category.parent_id.in_(ids)))).all():
        extra[parent_id].append(name)
    for cid, word in (await s.execute(select(Keyword.category_id, Keyword.word).where(
            Keyword.category_id.in_(ids), Keyword.kind.in_(("category", "strong"))))).all():
        extra[cid].append(word)
    scored = []
    for c in roots:
        name_hit = len(q & _meaningful(lemmas(c.name)))
        word_hit = len(q & _meaningful(lm for w in extra[c.id] for lm in lemmas(w)))
        if name_hit or word_hit:
            scored.append((name_hit * 2 + word_hit, -c.id, c))
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [c for *_, c in scored[:limit]]


async def similar(s: AsyncSession, text: str, limit: int = 4) -> list[Category]:
    """Перед «Предложить свою нишу»: есть ли уже похожая (по названию и словам из предложения)."""
    name, words = parse(text)
    return await find(s, " ".join([name, *words]), limit)
