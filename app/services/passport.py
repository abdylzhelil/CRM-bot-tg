"""📋 Правила групп: разведчик по очереди читает правила ВСЕХ своих групп (описание, закреп, боты, админы),
ИИ (если подключён) или правила-слова решают, можно ли там рекламу, и собирают список «куда что можно».
Раз в сутки владельцу — сводка: сколько проверено, куда можно, где только через админа (с готовым текстом),
и проблемы (закрытые группы, удалённые и невышедшие посты).

Админам групп бот САМ не пишет: сообщения с аккаунта разведчика незнакомым людям — прямой путь к ограничению
аккаунта (PeerFlood). Бот готовит текст и кнопку «✍️ Написать @админу» — владелец отправляет одним нажатием.
"""
import re
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AdPost, Group, Setting, now

POLICIES = {  # код: (значок, подпись)
    "allowed": ("✅", "Можно рекламу"),
    "only_admin": ("🤝", "Только через админа"),
    "paid": ("💰", "Платно у админа"),
    "forbidden": ("⛔", "Реклама запрещена"),
    "unknown": ("❓", "Про рекламу ничего"),
}
RECHECK = timedelta(days=14)  # правила меняются нечасто
PER_ROUND = 3
DIGEST_KEY = "rules_digest_at"
_CONTACT = re.compile(r"(?:реклам\w*|по\s+вопросам|сотрудничеств\w*|размещени\w*)[^@\n]{0,60}@([A-Za-z]\w{3,31})", re.I)


def policy_from_words(verdict: str, about: str) -> str:
    """Без ИИ: по найденной фразе про рекламу (adposts.rules_verdict)."""
    low = (verdict or "").lower()
    if not verdict:
        return "unknown" if not about.strip() else "allowed"
    if "плат" in low or "стоимост" in low or "прайс" in low:
        return "paid"
    if "админ" in low or "согласов" in low or "по вопросам" in low:
        return "only_admin"
    return "forbidden"


def contact_from_text(text: str) -> str:
    m = _CONTACT.search(text or "")
    return m.group(1) if m else ""


def ready_message(g: Group) -> str:
    """Текст владельцу для админа группы (отправляет сам)."""
    return (f"Здравствуйте! Хочу разместить рекламу в вашей группе «{g.title}». "
            "Подскажите, пожалуйста, на каких условиях и сколько это стоит?")[:256]


async def due(s: AsyncSession, accounts: set[int], limit: int = PER_ROUND) -> list[int]:
    """Какие группы пора прочитать: ни разу не читали — первыми, остальные раз в 2 недели. Только где наш разведчик."""
    if not accounts:
        return []
    rows = (await s.scalars(select(Group.id).where(
        Group.status == "joined", Group.kind == "group", Group.account_id.in_(accounts),
        Group.rules_checked_at.is_(None) | (Group.rules_checked_at < now() - RECHECK))
        .order_by(Group.rules_checked_at.is_not(None), Group.rules_checked_at, Group.members.desc()).limit(limit))).all()
    return list(rows)


async def counts(s: AsyncSession) -> dict[str, int]:
    rows = (await s.execute(select(Group.rules_policy, func.count()).where(
        Group.status == "joined", Group.kind == "group").group_by(Group.rules_policy))).all()
    return {p or "": n for p, n in rows}


async def listing(s: AsyncSession, policy: str, limit: int = 15) -> list[Group]:
    return list((await s.scalars(select(Group).where(
        Group.status == "joined", Group.kind == "group", Group.rules_policy == policy)
        .order_by(Group.members.desc()).limit(limit))).all())


async def digest(s: AsyncSession, force: bool = False) -> str:
    """Сводка за сутки (или "" — рано / нечего сообщать). Отмечает время отправки (коммит — на вызывающем)."""
    row = await s.get(Setting, DIGEST_KEY)
    from datetime import datetime

    last = datetime.fromisoformat(row.value) if row and row.value else now() - timedelta(days=1)
    if not force and now() - last < timedelta(days=1):
        return ""
    fresh = (await s.scalars(select(Group).where(Group.rules_checked_at > last, Group.status == "joined")
                             .order_by(Group.members.desc()))).all()
    closed = await s.scalar(select(func.count()).select_from(Group).where(
        Group.status == "joined", Group.ad_closed_at.is_not(None))) or 0
    deleted = await s.scalar(select(func.count()).select_from(AdPost).where(
        AdPost.status == "deleted", AdPost.sent_at > last)) or 0
    failed = await s.scalar(select(func.count()).select_from(AdPost).where(
        AdPost.status == "failed", AdPost.scheduled_at > last)) or 0
    from app.services import groupcheck

    audit = await groupcheck.audit_stats(s, last)
    if not fresh and not (deleted or failed or audit["checked"]) and not force:
        return ""
    if row is None:
        s.add(Setting(key=DIGEST_KEY, value=now().isoformat()))
    else:
        row.value = now().isoformat()
    total = await counts(s)
    by = {}
    for g in fresh:
        by.setdefault(g.rules_policy or "unknown", []).append(g)
    lines = [f"📋 <b>Правила групп</b> — прочитано за сутки: {len(fresh)}"]
    for code, (icon, label) in POLICIES.items():
        items = by.get(code, [])
        if items:
            names = ", ".join(_name(g) for g in items[:5]) + (f" и ещё {len(items) - 5}" if len(items) > 5 else "")
            lines.append(f"{icon} {label}: {len(items)} — {names}")
    need = sum(len(by.get(c, [])) for c in ("only_admin", "paid"))
    if need:
        lines.append(f"\n🤝 В {need} группах реклама — через админа: в списке есть кнопка «✍️ Написать» и готовый текст.")
    problems = []
    if closed:
        problems.append(f"🔒 закрыто для сообщений (капча, «новичкам нельзя»): {closed}")
    if deleted:
        problems.append(f"🗑 рекламных постов удалили админы: {deleted}")
    if failed:
        problems.append(f"⚠️ постов не вышло: {failed}")
    if audit["checked"]:
        lines.append(f"\n🧹 Проверка групп: проверено {audit['checked']}, вышли из спамных {audit['left']}, "
                     f"оставили без заявок {audit['muted']} (🔍 Группы → 🧹 Проверка групп).")
    if problems:
        lines.append("\nПроблемы за сутки:\n" + "\n".join("• " + p for p in problems))
    left = total.get("", 0)
    lines.append("\nВсего: " + ", ".join(f"{POLICIES[c][0]} {total.get(c, 0)}" for c in POLICIES)
                 + (f", ⏳ ещё не прочитано {left}" if left else ""))
    return "\n".join(lines)


def _name(g: Group) -> str:
    import html

    return html.escape(g.title[:30])
