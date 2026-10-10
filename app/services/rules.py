"""Загрузка правил из БД с коротким кэшем (сбрасывается при правках из бота)."""
import time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Category, Keyword
from app.services.text import NodeRules, Rules, compile_phrases

_TTL = 60.0
_cache: tuple[float, Rules] | None = None


def invalidate() -> None:
    global _cache
    _cache = None


async def load_rules(session: AsyncSession) -> Rules:
    global _cache
    if _cache and time.monotonic() - _cache[0] < _TTL:
        return _cache[1]

    cats = {c.id: c for c in (await session.scalars(select(Category))).all()}
    kws = (await session.scalars(select(Keyword))).all()

    def depth(c: Category) -> int:
        return 0 if c.parent_id is None else 1

    def active(c: Category) -> bool:
        parent = cats.get(c.parent_id) if c.parent_id else None
        return c.enabled and (parent is None or parent.enabled)

    rules = Rules(
        intents=compile_phrases([k.word for k in kws if k.kind == "intent"]),
        negatives=compile_phrases([k.word for k in kws if k.kind == "negative" and k.category_id is None]),
    )
    for c in cats.values():
        if not active(c):
            continue
        # Подкатегория наследует слова родителя, но своя ценнее (см. match: score+depth).
        own = [k for k in kws if k.category_id == c.id]
        city = c.city or (cats[c.parent_id].city if c.parent_id else "")
        rules.nodes.append(
            NodeRules(
                id=c.id,
                parent_id=c.parent_id,
                depth=depth(c),
                name=f"{cats[c.parent_id].name} → {c.name}" if c.parent_id else c.name,
                city=compile_phrases([city])[0] if city else [],
                words=compile_phrases([k.word for k in own if k.kind == "category"]),
                strong=compile_phrases([k.word for k in own if k.kind == "strong"]),
                negatives=compile_phrases([k.word for k in own if k.kind == "negative"]),
            )
        )
    _cache = (time.monotonic(), rules)
    return rules
