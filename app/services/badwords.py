"""«Не те заявки» (25.10): по каким словам ниши приходят заявки, на которые люди жмут «🙈 Не моё» / «🙅 Исполнитель».

`leads.matched` — слова, по которым заявка попала в нишу. Здесь — сводка «слово → сколько заявок, сколько отклонено»
и выключение слова одной кнопкой (kind → «x_<kind>»: правила его не читают, вернуть — обратно).
"""
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Category, Delivery, Keyword, Lead, now
from app.services import rules
from app.services.text import lemmas

MIN_REJECTED = 2   # меньше — случайность
MIN_SHARE = 0.4    # доля «не моё» среди заявок по слову


async def suspects(s: AsyncSession, days: int = 14, limit: int = 15) -> list[dict]:
    """[{category_id, category, phrase, total, rejected, share, keyword_id}] — худшие слова первыми."""
    since = now() - timedelta(days=days)
    leads = (await s.execute(select(Lead.id, Lead.category_id, Lead.matched).where(
        Lead.created_at >= since, Lead.matched != "", Lead.category_id.is_not(None)))).all()
    if not leads:
        return []
    bad_ids = set((await s.scalars(select(Delivery.lead_id).where(
        Delivery.lead_id.in_([lid for lid, _, _ in leads]), Delivery.status.in_(("rejected", "spam"))))).all())
    agg: dict[tuple[int, str], list[int]] = {}
    for lid, cid, matched in leads:
        for phrase in {p.strip() for p in matched.split(",") if p.strip()}:
            a = agg.setdefault((cid, phrase), [0, 0])
            a[0] += 1
            a[1] += lid in bad_ids
    cats = {c.id: c for c in (await s.scalars(select(Category))).all()}
    kws = (await s.scalars(select(Keyword).where(Keyword.kind.in_(("category", "strong"))))).all()
    by_phrase = {(k.category_id, " ".join(lemmas(k.word))): k for k in kws}
    out = []
    for (cid, phrase), (total, rejected) in agg.items():
        if rejected < MIN_REJECTED or rejected / total < MIN_SHARE:
            continue
        kw = by_phrase.get((cid, phrase))
        cat = cats.get(cid)
        out.append({"category_id": cid, "category": cat.name if cat else "—", "phrase": phrase,
                    "word": kw.word if kw else phrase, "total": total, "rejected": rejected,
                    "share": rejected / total, "keyword_id": kw.id if kw else 0})
    return sorted(out, key=lambda x: (-x["rejected"], -x["share"]))[:limit]


async def toggle(s: AsyncSession, keyword_id: int) -> Keyword | None:
    """Выключить слово ниши (или включить обратно). Коммит — на вызывающем."""
    kw = await s.get(Keyword, keyword_id)
    if kw is None:
        return None
    kw.kind = kw.kind[2:] if kw.kind.startswith("x_") else f"x_{kw.kind}"
    rules.invalidate()
    return kw


async def disabled(s: AsyncSession) -> list[Keyword]:
    return list((await s.scalars(select(Keyword).where(Keyword.kind.in_(("x_category", "x_strong"))))).all())
