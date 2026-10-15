"""Условия групп для рекламы: «подпишись на канал», «пригласи N друзей».

Разведчик выполняет то, что честно может: подписывается на открытый канал и добавляет в группу НАШИХ других
разведчиков (их номера есть в базе). Капчи, кнопки «я не бот», закрытые ссылки-приглашения — не трогаем:
за обход проверок Telegram банит аккаунты. Приглашений — не больше INVITES_PER_DAY в сутки с аккаунта.
"""
import logging
import random
import re
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select

from app.db import Session
from app.models import ScoutAccount

log = logging.getLogger(__name__)

# «подпишитесь на @канал», «вступите в t.me/канал» (служебные имена вроде t.me/joinchat, t.me/+… — мимо)
_SUB = re.compile(r"(подпи\w*|вступ\w*|subscri\w*|join)[^\n]{0,80}?(?:@|t\.me/|telegram\.me/)([A-Za-z]\w{3,31})", re.I)
_LINK = re.compile(r"(?:https?://)?(?:t\.me|telegram\.me)/([A-Za-z]\w{3,31})(?![/\w])", re.I)
_INV = re.compile(r"(пригла\w*|добав\w*|invite|add)\D{0,30}?(\d{1,2})\s*(друз\w*|человек\w*|участник\w*|чел\b|людей|friend|member|people)",
                  re.I)
_SKIP = {"joinchat", "addlist", "share", "proxy", "socks", "addstickers", "c", "s", "iv"}
INVITES_PER_DAY = 5
MAX_INVITE = 5  # больше пяти людей «за право писать» — не наш случай
_invites: dict[tuple[int, date], int] = {}


def parse(text: str, urls: list[str] = ()) -> tuple[set[str], int]:
    """Что требует группа: (каналы для подписки, сколько человек пригласить)."""
    text = text or ""
    channels = {m.group(2) for m in _SUB.finditer(text)}
    if channels or re.search(r"подпи\w*|subscri\w*", text, re.I):  # кнопка «Подписаться» со ссылкой
        channels |= {m.group(1) for u in urls for m in [_LINK.search(u or "")] if m}
    channels = {c for c in channels if c.lower() not in _SKIP}
    m = _INV.search(text)
    need = int(m.group(2)) if m else 0
    return channels, (need if 0 < need <= MAX_INVITE else 0)


def _urls(msg) -> list[str]:
    out = []
    for row in getattr(msg, "buttons", None) or []:
        for b in row:
            url = getattr(b, "url", None)
            if url:
                out.append(url)
    return out


async def demands(client, entity, me_id: int, since: datetime, my_ids: set[int] = frozenset()) -> tuple[set[str], int]:
    """Что боты группы написали НАМ (упомянули, ответили на наш пост) с момента since."""
    channels, need = set(), 0
    since = since.replace(tzinfo=timezone.utc) if since.tzinfo is None else since
    async for msg in client.iter_messages(entity, limit=40):
        if msg.date and msg.date < since:
            break
        sender = getattr(msg, "sender", None)
        if not (getattr(sender, "bot", False) or getattr(msg, "via_bot_id", None)):
            continue
        reply = getattr(getattr(msg, "reply_to", None), "reply_to_msg_id", None)
        if not (getattr(msg, "mentioned", False) or reply in my_ids or str(me_id) in (msg.message or "")):
            continue
        ch, n = parse(msg.message or "", _urls(msg))
        channels |= ch
        need = max(need, n)
    return channels, need


async def subscribe(client, channels: set[str]) -> list[str]:
    """Подписаться на открытые каналы (не больше двух за раз). Возвращает @имена, на которые подписались."""
    from telethon import functions

    done = []
    for name in list(channels)[:2]:
        try:
            ent = await client.get_entity(name)
            if not getattr(ent, "broadcast", False) and not getattr(ent, "megagroup", False):
                continue  # это человек или бот, а не канал/группа
            await client(functions.channels.JoinChannelRequest(ent))
            done.append(name)
        except Exception as e:
            log.info("условие группы: не подписался на %s (%s)", name, type(e).__name__)
    return done


async def invite_scouts(manager, inviter_acc_id: int, client, entity, need: int) -> int:
    """Пригласить в группу наших других разведчиков (по номерам из базы). Возвращает, скольких добавили."""
    from telethon import functions
    from telethon.tl.types import InputPhoneContact

    key = (inviter_acc_id, date.today())
    left = INVITES_PER_DAY - _invites.get(key, 0)
    if need <= 0 or left <= 0:
        return 0
    async with Session() as s:
        others = (await s.scalars(select(ScoutAccount).where(
            ScoutAccount.id != inviter_acc_id, ScoutAccount.enabled, ScoutAccount.phone != ""))).all()
    added = 0
    for acc in others[:min(need, left)]:
        try:
            res = await client(functions.contacts.ImportContactsRequest([InputPhoneContact(
                client_id=random.randint(1, 2**31), phone=acc.phone, first_name=acc.name[:60], last_name="")]))
            if not res.users:
                continue
            await client(functions.channels.InviteToChannelRequest(entity, [res.users[0]]))
            added += 1
        except Exception as e:
            log.info("условие группы: не пригласил разведчика #%s (%s)", acc.id, type(e).__name__)
    _invites[key] = _invites.get(key, 0) + added
    return added


async def fulfill(manager, acc_id: int, client, entity, me_id: int, since: datetime, my_ids: set[int] = frozenset(),
                  rules_text: str = "") -> list[str]:
    """Найти условия (сообщения ботов + правила группы) и выполнить, что можем. Возвращает, что сделали (для отчёта)."""
    channels, need = await demands(client, entity, me_id, since, my_ids)
    if rules_text:
        ch2, n2 = parse(rules_text)
        channels |= ch2
        need = max(need, n2)
    did = []
    for name in await subscribe(client, channels):
        did.append(f"подписался на @{name}")
    added = await invite_scouts(manager, acc_id, client, entity, need)
    if added:
        did.append(f"пригласил наших разведчиков: {added}")
    elif need:
        did.append(f"группа просит пригласить {need} чел. — наших свободных разведчиков не хватило")
    return did


def recent(hours: int = 72) -> datetime:
    return datetime.now(timezone.utc) - timedelta(hours=hours)
