"""Проверка группы разведчиком: хорошая ли она (живые люди, объявления, просьбы об услугах) или свалка спама,
заработка и рекламы; и какой у неё охват — город, регион или вся страна.

Сначала бесплатные правила (название, доля рекламы, «заработок/серые темы»), затем — если подключён ИИ
(GigaChat/YandexGPT/…, флаг `ai_decide`) — короткий вопрос ИИ по названию и последним сообщениям.
ИИ получает только тексты сообщений, без имён и ников авторов.

Где используется: перед вступлением (`joiner._quality`) и в перепроверке уже вступивших (`publisher.audit_round`):
спамные группы разведчик покидает (кроме каталога, рекламы и групп, найденных по просьбе пользователя).
"""
import re
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Group, now
from app.services import geo, junk

PER_ROUND = 3                 # групп за один заход перепроверки
RECHECK = timedelta(days=30)  # уже проверенную группу — раз в месяц
EARN_SHARE = 0.4              # столько и больше сообщений про заработок/серые темы — группа-спам
SCOPES = {"city": ("🏙", "Город"), "region": ("🗺", "Регион"), "country": ("🇷🇺", "Вся страна"), "": ("❓", "Не определено")}
_COUNTRY = re.compile(r"росси|\bрф\b|по всей стран|вся стран|всероссийск|\bснг\b|удал[её]нн|онлайн", re.I)
_EARN = re.compile(
    r"заработ\w*|подработк\w*|доход\w*|без вложени\w*|пассивн\w*|крипт\w*|инвестиц\w*|"
    r"#?работа\s*(в\s*)?любом\s*город\w*|#работавлюбомгороде|удал[её]нн\w+\s+работ\w*|"
    r"схем\w+\s+заработ\w*|арбитраж\w*|p2p|ставк\w*|казино|"
    r"(доход|зп|оплата)\s*от\s*\d", re.I)


def earn_reason(texts: list[str]) -> str:
    """Группа, где большая часть сообщений — «заработок», крипта, серые темы."""
    from app.services import antispam

    texts = [x for x in texts if len((x or "").split()) >= 5]
    if len(texts) < junk.MIN_TEXTS:
        return ""
    bad = sum(bool(_EARN.search(x) or antispam._GREY.search(x)) for x in texts)
    if bad >= len(texts) * EARN_SHARE:
        return f"заработок/серые темы ({bad} из {len(texts)})"
    return ""


def place(title: str, about: str = "") -> dict:
    """Охват по названию и описанию, без ИИ."""
    city, region = geo.find(f"{title}\n{about}")
    if city:
        return {"scope": "city", "city": city, "region": region}
    if region:
        return {"scope": "region", "city": "", "region": region}
    if _COUNTRY.search(f"{title}\n{about}"):
        return {"scope": "country", "city": "", "region": ""}
    return {"scope": "", "city": "", "region": ""}


def by_rules(title: str, about: str, texts: list[str]) -> dict:
    reason = junk.title_reason(title) or junk.content_reason(texts) or earn_reason(texts)
    return {"verdict": "spam" if reason else "good", "reason": reason, "ai": False, **place(title, about)}


SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["verdict", "scope", "city", "region", "reason"],
    "properties": {
        "verdict": {"type": "string", "enum": ["good", "spam", "offtopic"]},
        "scope": {"type": "string", "enum": ["city", "region", "country", "unknown"]},
        "city": {"type": "string"}, "region": {"type": "string"}, "reason": {"type": "string"},
    },
}
SYSTEM = (
    "Ты помогаешь сервису, который ищет в Telegram-группах заказчиков услуг (ремонт, юристы, репетиторы, доставка и т. п.). "
    "Даны название группы, описание и последние сообщения. Ответь JSON:\n"
    "verdict: good — живая группа людей (город, район, барахолка, объявления, просьбы о помощи и услугах), "
    "там могут быть заказчики; spam — почти одна реклама, «заработок», крипта, ставки, интим, взаимопиар, "
    "приглашения в другие чаты; offtopic — живая, но заказчиков услуг там не будет (игры, фанаты, учёба одного класса, "
    "переписка на чужом языке);\n"
    "scope: city — группа одного города или района; region — области/республики/края; country — вся страна или "
    "без места; unknown — непонятно;\n"
    "city: город по-русски (пусто, если не город); region: регион по-русски (например «Дагестан», "
    "«Московская область»; пусто, если не понятно);\n"
    "reason: 3–8 слов по-русски, почему такой вывод."
)


async def assess(s: AsyncSession, title: str, about: str, texts: list[str], client=None) -> dict:
    """{"verdict": good|spam|offtopic, "reason", "scope", "city", "region", "ai"}. Коммит сессии — внутри ИИ-запроса."""
    from app.services import brain

    out = by_rules(title, about, texts)
    if out["verdict"] == "spam" or not texts:
        return out  # правила уже уверены (или судить не по чему) — ИИ не тратим
    sample = "\n---\n".join(t[:300] for t in texts if t and t.strip())[:4500]
    data = f"Название: {title[:120]}\nОписание: {about[:600]}\n\nСообщения:\n{sample}"
    ans = await brain.ask(s, "group", SYSTEM, data, SCHEMA, client)
    if not ans:
        return out
    out["ai"] = True
    if ans.get("verdict") in ("spam", "offtopic"):
        out["verdict"], out["reason"] = ans["verdict"], ("ИИ: " + (ans.get("reason") or "не подходит"))[:110]
    if not out["scope"]:  # место, найденное по словам, надёжнее; ИИ дополняет
        scope = ans.get("scope") if ans.get("scope") in ("city", "region", "country") else ""
        city = (ans.get("city") or "").strip()[:64]
        region = (ans.get("region") or "").strip()[:64] or geo.region_of(city)
        if scope == "city" and not city:
            scope = "region" if region else ""
        out.update(scope=scope, city=city if scope == "city" else "", region=region if scope != "country" else "")
    return out


def apply_place(g: Group, res: dict) -> None:
    """Записать охват в группу. Город, который поставил человек (каталог), не трогаем."""
    if not res.get("scope"):
        return
    g.scope = res["scope"]
    if res.get("region"):
        g.region = res["region"][:64]
    if res.get("city") and not g.city:
        g.city = res["city"][:64]


async def due(s: AsyncSession, accounts: set[int], limit: int = PER_ROUND) -> list[int]:
    """Вступившие группы разведчика, которые пора перепроверить: ни разу — первыми, остальные раз в месяц."""
    if not accounts:
        return []
    return list((await s.scalars(select(Group.id).where(
        Group.status == "joined", Group.kind == "group", Group.account_id.in_(accounts),
        Group.audit_at.is_(None) | (Group.audit_at < now() - RECHECK))
        .order_by(Group.audit_at.is_not(None), Group.audit_at, Group.members.desc()).limit(limit))).all())


def protected(g: Group) -> str:
    """Почему из группы нельзя выходить автоматически ("" — можно)."""
    if g.catalog or g.submitted_by:
        return "группа из каталога"
    if g.ads_allowed:
        return "в ней разрешена реклама"
    if g.wanted_by:
        return "её просил пользователь"
    return ""


async def scope_counts(s: AsyncSession) -> dict[str, int]:
    rows = (await s.execute(select(Group.scope, func.count()).where(
        Group.status == "joined", Group.kind == "group").group_by(Group.scope))).all()
    return {k or "": n for k, n in rows}


async def places(s: AsyncSession, scope: str, limit: int = 12) -> list[tuple[str, int]]:
    col = Group.city if scope == "city" else Group.region
    rows = (await s.execute(select(col, func.count()).where(
        Group.status == "joined", Group.kind == "group", Group.scope == scope, col != "")
        .group_by(col).order_by(func.count().desc()).limit(limit))).all()
    return [(p, n) for p, n in rows]


async def audit_stats(s: AsyncSession, since) -> dict[str, int]:
    """Сколько перепроверено и из скольких вышли/отключили заявки с момента since."""
    checked = await s.scalar(select(func.count()).select_from(Group).where(Group.audit_at > since)) or 0
    left = await s.scalar(select(func.count()).select_from(Group).where(
        Group.audit_at > since, Group.status == "left", Group.audit_verdict.in_(("spam", "offtopic")))) or 0
    muted = await s.scalar(select(func.count()).select_from(Group).where(
        Group.audit_at > since, Group.status == "joined", Group.no_leads, Group.audit_verdict.in_(("spam", "offtopic")))) or 0
    return {"checked": checked, "left": left, "muted": muted}
