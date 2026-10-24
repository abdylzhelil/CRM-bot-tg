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
