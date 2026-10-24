"""🧩 Капча после вступления: бот-модератор группы просит «я не бот» — пока не пройдёшь, писать нельзя.

ИИ и разведчик капчи НЕ проходят (Telegram за это блокирует аккаунт). Бот только находит такие группы,
даёт владельцу ссылку на сообщение бота с проверкой (открыть с телефона разведчика и пройти руками)
и, если за `captcha_leave_days` дней проверку не прошли, сам выходит из группы.
Заявки из такой группы читаются и до прохождения — капча мешает только писать (реклама).
"""
import asyncio
import html
import logging
import random
import re
from datetime import timedelta, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from telethon import functions
from telethon.errors import ChannelPrivateError, UserBannedInChannelError

from app.bot import notify
from app.db import Session
from app.models import Group, now
from app.services import settings

log = logging.getLogger(__name__)

PER_ROUND = 5
FRESH = timedelta(days=3)       # недавно вступившие — проверяем, не просит ли бот капчу
RECHECK = timedelta(hours=1)    # одну группу — не чаще раза в час
_WORDS = re.compile(r"капч|captcha|не\s*бот|not\s*a?\s*bot|робот|robot|подтверд|verify|verification|нажмите\s+кнопк|"
                    r"докаж|проверк\w+\s+(на|что)|человек", re.I)


# боты-капчи, которые удаляют не прошедших проверку за минуты: вступать туда — потерять вступление
_KICK_BOT = re.compile(r"captcha|capcha|kapcha|shieldy|join_?check|verif\w*bot|human\w*bot|antibot|anti_bot", re.I)
_KICK_ABOUT = re.compile(r"капч|captcha|@\w*(captcha|shieldy)\w*", re.I)


def kick_reason(full) -> str:
    """По ответу GetFullChannel (виден без вступления): есть ли в группе бот-капча. "" — нет."""
    bots = [getattr(u, "username", "") or "" for u in (getattr(full, "users", None) or []) if getattr(u, "bot", False)]
    hit = next((b for b in bots if _KICK_BOT.search(b)), "")
    if hit:
        return f"капча: бот @{hit}"[:120]
    about = getattr(getattr(full, "full_chat", None), "about", "") or ""
    return "капча: в описании группы" if _KICK_ABOUT.search(about) else ""


async def admin_text(s: AsyncSession, g: Group) -> str:
    """Готовый текст админу группы (отправляет владелец сам) — общий с «🤝 Предложить бота админам»."""
    from app.services import botoffer

    return await botoffer.text(s, g)


def restricted(ent) -> bool:
    """Ограничен ли именно этот аккаунт (капча/«новичкам нельзя»). Закрытая для всех группа — не капча."""
    own = getattr(ent, "banned_rights", None)
    return bool(own is not None and (getattr(own, "send_messages", False) or getattr(own, "send_plain", False)))


def _is_check(msg, me_id: int) -> int:
    """Насколько сообщение похоже на проверку для нас: 2 — бот обратился к нам, 1 — бот с кнопками/словами про капчу."""
    sender = getattr(msg, "sender", None)
    if not (getattr(sender, "bot", False) or getattr(msg, "via_bot_id", None)):
        return 0
    text = msg.message or ""
    if getattr(msg, "mentioned", False) or (me_id and str(me_id) in text):
        return 2
    return 1 if _WORDS.search(text) else 0


async def find_message(client, entity, me_id: int, since) -> int:
    """Номер сообщения бота с проверкой (0 — не нашли)."""
    since = since.replace(tzinfo=timezone.utc) if since and since.tzinfo is None else since
    best, best_score = 0, 0
    try:
        async for msg in client.iter_messages(entity, limit=60):
            if since and msg.date and msg.date < since - timedelta(minutes=5):
                break
            score = _is_check(msg, me_id)
            if score > best_score:
                best, best_score = msg.id, score
                if score == 2:
                    break
    except Exception:
        log.info("капча: не прочитал сообщения группы", exc_info=True)
    return best


async def due(s: AsyncSession, accounts: set[int], limit: int = PER_ROUND) -> list[int]:
    if not accounts:
        return []
    return list((await s.scalars(select(Group.id).where(
        Group.status == "joined", Group.kind == "group", Group.account_id.in_(accounts),
        or_(Group.joined_at > now() - FRESH, Group.captcha_since.is_not(None), Group.ad_closed_at.is_not(None)),
        or_(Group.captcha_checked_at.is_(None), Group.captcha_checked_at < now() - RECHECK))
        .order_by(Group.captcha_checked_at.is_not(None), Group.captcha_checked_at).limit(limit))).all())


async def count(s: AsyncSession) -> int:
    return await s.scalar(select(func.count()).select_from(Group).where(
        Group.status == "joined", Group.captcha_since.is_not(None))) or 0


async def listing(s: AsyncSession, limit: int = 20) -> list[Group]:
    return list((await s.scalars(select(Group).where(Group.status == "joined", Group.captcha_since.is_not(None))
                                 .order_by(Group.captcha_since).limit(limit))).all())


async def _kicked(s: AsyncSession, g: Group) -> str:
    """Разведчика удалили из группы. Если там была капча — группу не теряем: «🖐 Вступить вручную»."""
    if g.captcha_since is not None:
        g.status, g.skip_reason = "manual", "бот-капча удалил разведчика — вступить вручную"
    else:
        g.status, g.skip_reason = "left", "удалили из группы (бан или чистка)"
    g.captcha_since, g.captcha_link = None, ""
    await s.commit()
    if g.status == "manual":
        await tell_manual(g.id)
    return "kicked"


async def restore_skipped(s: AsyncSession) -> int:
    """Один раз: группы, пропущенные из-за капчи прежней версией, — в «🖐 Вступить вручную» (не теряем)."""
    from app.models import Setting

    if await s.get(Setting, "captcha_manual_v1") is not None:
        return 0
    rows = (await s.scalars(select(Group).where(Group.status == "skipped", Group.skip_reason.like("капча%")))).all()
    for g in rows:
        g.status = "manual"
    s.add(Setting(key="captcha_manual_v1", value="1"))
    await s.commit()
    return len(rows)


async def manual_count(s: AsyncSession) -> int:
    return await s.scalar(select(func.count()).select_from(Group).where(Group.status == "manual")) or 0


async def tell_manual(gid: int) -> None:
    """Одна группа ушла в «🖐 Вступить вручную» — сообщить владельцу с кнопкой."""
    async with Session() as s:
        g = await s.get(Group, gid)
        if g is None:
            return
        n = await manual_count(s)
    rows = []
    if group_url(g):
        rows.append([notify.InlineKeyboardButton(text="🔗 Открыть группу", url=group_url(g))])
    rows.append([notify.InlineKeyboardButton(text=f"🖐 Вступить вручную · {n}", callback_data="gr:man")])
    await notify._send(f"🖐 «{html.escape(g.title)}» ({g.members} уч.) — {html.escape(g.skip_reason)}.\n"
                       "Чтобы не терять группу, вступите в неё <b>с телефона разведчика</b> и пройдите проверку сами, "
                       "затем «✅ Вступил» в списке.", notify.InlineKeyboardMarkup(inline_keyboard=rows))


async def find_member(manager, g: Group):
    """Какой из наших разведчиков уже состоит в группе (после ручного вступления). (acc_id, ограничен ли) или (None, False)."""
    from app.scout.publisher import resolve

    order = [g.account_id] + [a for a in manager.clients if a != g.account_id] if g.account_id else list(manager.clients)
    for acc_id in order:
        client = manager.clients.get(acc_id)
        if client is None:
            continue
        try:
            ent = await client.get_entity(await resolve(client, g))
        except Exception:
            continue
        if not getattr(ent, "left", True):
            return acc_id, restricted(ent)
    return None, False


def has_leads(g: Group) -> bool:
    """Заявки из группы шли, пока висела капча: читать её полезно — не выходим."""
    return bool(g.captcha_since and g.last_lead_at and g.last_lead_at >= g.captcha_since)


def leave_at(g: Group, days: int):
    return g.captcha_since + timedelta(days=days) if g.captcha_since and days > 0 else None


async def leave(client, g: Group, reason: str) -> bool:
    try:
        await client(functions.channels.LeaveChannelRequest(await client.get_input_entity(g.tg_id)))
    except Exception:
        log.info("капча: не вышел из %s", g.title, exc_info=True)
        return False
    g.status, g.skip_reason, g.captcha_since = "left", reason, None
    return True


async def check_one(manager, gid: int) -> str:
    """captcha (новая) / still / passed / ok / kicked / left / skip."""
    from app.scout.publisher import exact_link, resolve
    from app.services import groupcheck

    async with Session() as s:
        g = await s.get(Group, gid)
        if g is None or g.status != "joined":
            return "skip"
        client = manager.clients.get(g.account_id)
        days = await settings.get_int(s, "captcha_leave_days")
        await s.commit()  # не держим базу, пока ждём Telegram
        if client is None:
            return "skip"
        g.captcha_checked_at = now()
        try:
            ent = await client.get_entity(await resolve(client, g))
        except (ChannelPrivateError, UserBannedInChannelError):
            return await _kicked(s, g)
        except Exception:
            await s.commit()
            return "skip"
        if getattr(ent, "left", False):
            return await _kicked(s, g)
        if not restricted(ent):
            was = g.captcha_since is not None
            if was and g.ad_closed_at and g.ad_closed_at >= g.captcha_since:
                g.ad_closed_at = None  # закрыта была из-за капчи — для рекламы снова открыта
            g.captcha_since, g.captcha_link = None, ""
            await s.commit()
            return "passed" if was else "ok"
        if g.captcha_since is None:
            g.captcha_since = now()
            g.ad_closed_at = g.ad_closed_at or g.captcha_since  # писать нельзя — реклама сюда не идёт, пока не пройдёте
            me = await client.get_me()
            mid = await find_message(client, ent, getattr(me, "id", 0), g.joined_at)
            g.captcha_link = (await exact_link(client, ent, g, mid) if mid else "")[:255]
            await s.commit()
            return "captcha"
        expire = leave_at(g, days)
        if expire and now() >= expire and not groupcheck.protected(g) and not has_leads(g):
            await s.commit()
            ok = await leave(client, g, f"капчу не прошли за {days} дн.")
            await s.commit()
            return "left" if ok else "still"
        await s.commit()
        return "still"


async def captcha_round(manager) -> dict[str, int]:
    """Проверить следующие группы; о новых капчах — одно сообщение владельцу со ссылками."""
    async with Session() as s:
        ids = await due(s, set(manager.clients))
    out: dict[str, int] = {}
    fresh = []
    for gid in ids:
        try:
            r = await check_one(manager, gid)
        except Exception:
            log.exception("капча: группа %s", gid)
            r = "skip"
        out[r] = out.get(r, 0) + 1
        if r == "captcha":
            fresh.append(gid)
        await asyncio.sleep(random.uniform(2, 5))
    if fresh:
        await tell(fresh)
    return out


async def tell(gids: list[int]) -> None:
    async with Session() as s:
        groups = (await s.scalars(select(Group).where(Group.id.in_(gids)))).all()
        days = await settings.get_int(s, "captcha_leave_days")
    if not groups:
        return
    lines = ["🧩 <b>Нужно пройти капчу</b> — бот-модератор просит разведчика подтвердить, что он не бот. "
             "Откройте ссылку <b>с телефона разведчика</b> и нажмите кнопку проверки."]
    for g in groups:
        lines.append(f"• {html.escape(g.title)}" + ("" if g.captcha_link else " — сообщение бота не нашёл, откройте группу"))
    if days:
        lines.append(f"\nНе пройдёте за {days} дн. и заявок оттуда не будет — разведчик сам выйдет из группы. "
                     "Или предложите админу бесплатного бота-антиспама: «🧩 Все группы с капчей» → «📋 Текст админу».")
    rows = [[notify.InlineKeyboardButton(text=f"🧩 {g.title[:30]}", url=g.captcha_link or group_url(g))]
            for g in groups[:8] if g.captcha_link or group_url(g)]
    rows.append([notify.InlineKeyboardButton(text="🧩 Все группы с капчей", callback_data="gr:cap")])
    await notify._send("\n".join(lines), notify.InlineKeyboardMarkup(inline_keyboard=rows))


def group_url(g: Group) -> str:
    return f"https://t.me/{g.username}" if g.username else ""
