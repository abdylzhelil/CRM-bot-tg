"""Срок хранения заявок и удаление данных автора заявки по его просьбе.

В заявках (leads) лежат тексты сообщений людей из групп, их имя и публичный ник. Эти люди не пользователи бота, поэтому:
- старые заявки удаляются сами через lead_keep_days (по умолчанию 90). Остаются только те, из которых пользователь сделал клиента (✅ В работу);
- администратор может удалить всё об авторе: /forget (заявки, их доставки, голоса «спам», запись в чёрном списке; карточки в CRM пользователей обезличиваются).
"""
from datetime import timedelta

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import BlacklistEntry, Client, ClientEvent, Delivery, Lead, SpamVote, User, now

CHUNK = 500


async def purge_old(s: AsyncSession, days: int) -> int:
    """Удалить заявки старше days дней (кроме взятых в работу). 0 — не удалять. Возвращает число удалённых заявок."""
    if days <= 0:
        return 0
    taken = select(Delivery.lead_id).where(Delivery.status == "taken")
    ids = list((await s.scalars(select(Lead.id).where(Lead.created_at < now() - timedelta(days=days), Lead.id.not_in(taken)))).all())
    for i in range(0, len(ids), CHUNK):
        part = ids[i:i + CHUNK]
        await s.execute(delete(Delivery).where(Delivery.lead_id.in_(part)))
        await s.execute(delete(Lead).where(Lead.id.in_(part)))
    await s.commit()
    return len(ids)


async def find_authors(s: AsyncSession, query: str) -> list[tuple[int, str]]:
    """По ID или @нику: [(id автора, ник)]. Ищет среди авторов заявок и записей чёрного списка."""
    q = query.strip()
    raw = q.lstrip("@")
    if not raw:
        return []
    if raw.isdigit():
        return [(int(raw), await _username(s, int(raw)))]
    found: dict[int, str] = {}
    for model in (Lead, BlacklistEntry):
        col = model.author_id if model is Lead else model.user_id
        rows = (await s.execute(select(col, model.username).where(model.username.ilike(raw)))).all()
        for uid, uname in rows:
            found[uid] = uname
    return list(found.items())


async def _username(s: AsyncSession, author_id: int) -> str:
    name = await s.scalar(select(Lead.username).where(Lead.author_id == author_id, Lead.username != "").limit(1))
    return name or await s.scalar(select(BlacklistEntry.username).where(BlacklistEntry.user_id == author_id)) or ""


async def author_stats(s: AsyncSession, author_id: int) -> dict:
    lead_ids = select(Lead.id).where(Lead.author_id == author_id)
    count = lambda *a: s.scalar(select(func.count()).select_from(*a[:1]).where(*a[1:]))  # noqa: E731
    return {
        "leads": await count(Lead, Lead.author_id == author_id) or 0,
        "deliveries": await s.scalar(select(func.count()).select_from(Delivery).where(Delivery.lead_id.in_(lead_ids))) or 0,
        "crm": await count(Client, Client.tg_id == author_id) or 0 if author_id else 0,
        "blacklist": bool(await s.get(BlacklistEntry, author_id)),
        "is_user": await s.get(User, author_id) is not None,
    }


async def forget_author(s: AsyncSession, author_id: int) -> dict:
    """Удалить всё об авторе. Карточки в CRM пользователей не удаляются, а обезличиваются (чтобы у них остались бюджет и этап)."""
    stats = await author_stats(s, author_id)
    lead_ids = select(Lead.id).where(Lead.author_id == author_id)
    await s.execute(delete(Delivery).where(Delivery.lead_id.in_(lead_ids)))
    await s.execute(delete(Lead).where(Lead.author_id == author_id))
    await s.execute(delete(SpamVote).where(SpamVote.author_id == author_id))
    await s.execute(delete(BlacklistEntry).where(BlacklistEntry.user_id == author_id))
    if author_id:
        cards = list((await s.scalars(select(Client.id).where(Client.tg_id == author_id))).all())
        if cards:
            await s.execute(delete(ClientEvent).where(ClientEvent.client_id.in_(cards)))
            await s.execute(update(Client).where(Client.id.in_(cards)).values(
                name="Удалён по просьбе", username="", phone="", notes="", source="", tg_id=0))
    await s.commit()
    return stats
