"""🤝 Предложить бота админам групп: готовый текст + ссылка, которая открывает чат с админом с уже вставленным текстом.

Отправляет ВЛАДЕЛЕЦ со своего аккаунта одним нажатием «Отправить». Разведчик сам незнакомым НЕ пишет:
Telegram за личные сообщения незнакомым с аккаунта-разведчика ограничивает его (PeerFlood) — пропадёт поиск заявок.
Статусы: "" — не предлагали, sent — отправлено, bot — поставили бота, ads — договорились о рекламе, no — отказ, skip — не сейчас.
"""
from urllib.parse import quote

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Group, now
from app.services import settings

STATUSES = {"sent": "📨 Ждём ответа", "bot": "🛡 Поставили бота", "ads": "🤝 Договорились о рекламе",
            "no": "👎 Отказались", "skip": "⏭ Не сейчас"}
_ALIVE = ("joined", "manual")


def contact(g: Group) -> str:
    """С кем говорить: указанный в правилах контакт, иначе первый админ."""
    if g.rules_contact:
        return g.rules_contact
    return next((a.strip() for a in (g.admins or "").split(",") if a.strip()), "")


async def text(s: AsyncSession, g: Group) -> str:
    bot = await settings.get(s, "mod_bot_username")
    who = f"@{bot}" if bot else "нашего бота"
    head = (f"Здравствуйте! Пишу по группе «{g.title[:40]}». "
            + ("Ваша капча не пропускает наш аккаунт. " if g.captcha_since or g.status == "manual" else ""))
    return (head + f"Предлагаю бесплатно поставить {who} — убирает спам и рекламу. "
            "Также готовы обсудить платную рекламу у вас. Интересно?")[:256]


def link(username: str, message: str) -> str:
    """Чат с человеком, где текст уже вставлен — остаётся нажать «Отправить»."""
    return f"https://t.me/{username}?text={quote(message)}"


def _base():
    return select(Group).where(Group.status.in_(_ALIVE), Group.kind == "group",
                               or_(Group.rules_contact != "", Group.admins != ""))


async def todo(s: AsyncSession, limit: int = 8) -> list[Group]:
    """Кому ещё не предлагали: сначала группы с капчей, потом самые большие."""
    return list((await s.scalars(_base().where(Group.offer_status == "")
                                 .order_by(Group.captcha_since.is_(None), Group.status != "manual", Group.members.desc())
                                 .limit(limit))).all())


async def listing(s: AsyncSession, status: str, limit: int = 15) -> list[Group]:
    return list((await s.scalars(select(Group).where(Group.offer_status == status)
                                 .order_by(Group.offer_at.desc()).limit(limit))).all())


async def counts(s: AsyncSession) -> dict[str, int]:
    rows = dict((await s.execute(select(Group.offer_status, func.count()).where(Group.offer_status != "")
                                 .group_by(Group.offer_status))).all())
    rows[""] = await s.scalar(select(func.count()).select_from(_base().where(Group.offer_status == "").subquery())) or 0
    return rows


async def mark(s: AsyncSession, g: Group, status: str) -> None:
    """Отметить итог. «Договорились о рекламе» — правила больше не мешают рекламе (как «🤝 Договорился с админом»)."""
    g.offer_status, g.offer_at = status, now()
    if status == "ads":
        g.ad_agreed = True


STALE = 7  # дней без ответа — «⏭ Не сейчас» (можно вернуть)


async def sweep(s: AsyncSession) -> int:
    """Авто-отметки: бот уже в группе (наш КлиентРадар или бот модерации) → «поставили бота»; без ответа 7 дней → «не сейчас»."""
    from datetime import timedelta

    from app.models import FeedChat

    n = 0
    feed = set((await s.scalars(select(FeedChat.tg_id))).all())
    for g in (await s.scalars(select(Group).where(Group.offer_status.in_(("", "sent"))))).all():
        if g.bot_member or g.tg_id in feed:
            await mark(s, g, "bot")
            n += 1
        elif g.offer_status == "sent" and g.offer_at and now() - g.offer_at > timedelta(days=STALE):
            await mark(s, g, "skip")
            n += 1
    return n


async def daily(bot) -> int:
    """Раз в день (после 10:00 по местному времени) — offer_daily готовых предложений владельцу, одним сообщением на группу."""
    from app.db import Session
    from app.models import Setting
    from app.services import quiet

    local = quiet.to_local(now())
    if local.hour < 10:
        return 0
    async with Session() as s:
        row = await s.get(Setting, "offer_batch_at")
        today = local.date().isoformat()
        if row is not None and row.value == today:
            return 0
        if row is None:
            s.add(Setting(key="offer_batch_at", value=today))
        else:
            row.value = today
        await sweep(s)
        limit = await settings.get_int(s, "offer_daily")
        groups = await todo(s, limit) if limit else []
        texts = {g.id: await text(s, g) for g in groups}
        await s.commit()
    if not groups:
        return 0
    from app.bot import notify
    from app.bot.handlers import groups as groups_h

    await notify._send(f"🤝 <b>Предложения админам на сегодня: {len(groups)}</b>\n"
                       "Нажмите «✉️ Написать» — текст уже вставлен, остаётся «Отправить», затем «✅ Отправил». "
                       "Пишете со своего аккаунта, не больше 5–10 в день.")
    for g in groups:
        body, markup = groups_h._offer_card(g, texts[g.id])
        await notify._send(body, markup)
    return len(groups)
