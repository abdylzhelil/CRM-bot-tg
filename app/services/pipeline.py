"""Сообщение из группы -> триггеры -> антиспам -> антидубли -> Lead."""
import difflib
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import BlacklistEntry, FilteredMessage, Group, Lead, ScoutAccount, SeenMessage, SpamSample, now
from app.services import geo, offers, settings, spamtexts
from app.services.antispam import AuthorInfo, Behavior, score_message, text_hash
from app.services.rules import load_rules
from app.services.text import match


@dataclass
class Incoming:
    text: str
    author: AuthorInfo
    group_tg_id: int
    group_title: str
    message_id: int
    link: str = ""


async def process(session: AsyncSession, msg: Incoming, force: bool = False) -> Lead | None:
    """Возвращает новый Lead или None (не заявка / спам / дубль)."""
    author = msg.author
    if author.user_id and await session.scalar(select(ScoutAccount.id).where(ScoutAccount.tg_user_id == author.user_id)):
        return None  # это наш разведчик (например, его рекламный пост) — не заявка
    entry = await session.get(BlacklistEntry, author.user_id)
    if entry and entry.status == "spam":
        return None
    group = await session.scalar(select(Group).where(Group.tg_id == msg.group_tg_id))
    if group is not None and group.no_leads and not force:
        return None  # группа «без заявок» (реклама/флуд) — 🔍 Группы → 🙈 Без заявок

    m = match(msg.text, await load_rules(session), context=msg.group_title)
    if not m:
        return None

    # тот же спам с другого аккаунта: текст похож на уже отмеченный «спам» → не заявка, автор — в чёрный список
    sample_id = None if entry is not None and entry.status == "client" else await spamtexts.match(session, msg.text)
    if sample_id:
        sample = await session.get(SpamSample, sample_id)
        if sample is not None:
            sample.hits += 1
        if entry is None:
            session.add(BlacklistEntry(user_id=author.user_id, username=author.username, status="spam",
                                       reason=f"текст как спам-образец №{sample_id}"))
        await session.commit()
        return None

    h = text_hash(msg.text)
    since = now() - timedelta(minutes=10)
    behavior = Behavior(
        same_text_other_groups=await session.scalar(
            select(func.count(func.distinct(SeenMessage.group_id))).where(
                SeenMessage.text_hash == h,
                SeenMessage.group_id != msg.group_tg_id,
                SeenMessage.created_at > now() - timedelta(days=1),
            )
        )
        or 0,
        msgs_last_10min=await session.scalar(
            select(func.count()).select_from(SeenMessage).where(
                SeenMessage.author_id == author.user_id, SeenMessage.created_at > since
            )
        )
        or 0,
    )
    session.add(SeenMessage(text_hash=h, author_id=author.user_id, group_id=msg.group_tg_id))

    whitelisted = entry is not None and entry.status == "client"
    score = score_message(author, msg.text, behavior)
    value = 0 if whitelisted else score.value
    if value >= await settings.get_int(session, "spam_block"):
        if entry is None:
            session.add(
                BlacklistEntry(
                    user_id=author.user_id, username=author.username, status="spam",
                    reason=", ".join(score.reasons)[:255],
                )
            )
        await session.commit()
        return None

    # Антидубли: то же/почти то же сообщение этого автора за последние N дней.
    days = await settings.get_int(session, "dedupe_days")
    prev = await session.scalars(
        select(Lead.text).where(
            Lead.author_id == author.user_id, Lead.created_at > now() - timedelta(days=days)
        )
    )
    for old in prev:
        if difflib.SequenceMatcher(None, old.lower(), msg.text.lower()).ratio() >= 0.85:
            await session.commit()
            return None

    # исполнитель рекламирует свои услуги («Сделаю сайт за 3 дня…») — это не заказчик (force — админ вернул вручную)
    if not force and not whitelisted:
        kind, by = await offers.verdict(session, msg.text)
        if kind != "request":
            session.add(FilteredMessage(
                text=msg.text[:2000], author_id=author.user_id, username=author.username, first_name=author.first_name,
                group_tg_id=msg.group_tg_id, group_title=msg.group_title, message_id=msg.message_id, link=msg.link,
                reason=("реклама услуг" if kind == "offer" else "не заявка") + f" ({by})"))
            await session.commit()
            return None

    if group:
        group.leads_count += 1
        group.last_lead_at = now()
    lead = Lead(
        author_id=author.user_id,
        username=author.username,
        name=f"{author.first_name} {author.last_name}".strip(),
        group_id=group.id if group else None,
        group_title=msg.group_title,
        message_id=msg.message_id,
        link=msg.link,
        text=msg.text,
        text_hash=h,
        category_id=m.category_id,
        spam_score=value,
        suspicious=value >= await settings.get_int(session, "spam_suspicious"),
    )
    lead.city, lead.region = geo.detect(msg.text, msg.group_title, group.city if group else "",
                                         group.region if group else "")
    session.add(lead)
    await session.commit()
    return lead
