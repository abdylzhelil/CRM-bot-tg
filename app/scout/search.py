"""Поиск публичных групп по ключевым запросам."""
import asyncio
import logging
import random

from sqlalchemy import select
from telethon import TelegramClient, functions, utils
from telethon.errors import FloodWaitError
from telethon.tl.types import Channel

from app.bot import notify
from app.db import Session
from app.models import FeedChat, Group, SearchQuery, now
from app.scout.manager import public_username
from app.services import groupcheck, junk, settings

log = logging.getLogger(__name__)


def detect_lang(title: str) -> str:
    cyr = sum("а" <= c.lower() <= "я" or c.lower() == "ё" for c in title)
    return "ru" if cyr >= max(1, len(title) // 3) else "other"


async def _sample(client: TelegramClient, entity: Channel) -> tuple[float, list[str]]:
    """(сообщений в сутки по последним 100, их тексты); (-1, []) если прочитать нельзя."""
    try:
        msgs = [m async for m in client.iter_messages(entity, limit=100)]
    except Exception:
        return -1, []
    texts = [m.message for m in msgs if getattr(m, "message", None)]
    if len(msgs) < 2:
        return 0, texts
    span = (msgs[0].date - msgs[-1].date).total_seconds() / 86400
    return len(msgs) / max(span, 1 / 24), texts


async def _activity(client: TelegramClient, entity: Channel) -> float:
    return (await _sample(client, entity))[0]


_stop_requested = False


def request_stop() -> None:
    """Кнопка «⏹ Остановить поиск»: текущий проход завершится после ближайшего шага."""
    global _stop_requested
    _stop_requested = True


async def is_covered(s, tg_id: int) -> bool:
    """Общая база: в группе уже есть наш бот (CRM-бот, бот модерации или другой разведчик)."""
    if await s.get(FeedChat, tg_id) is not None:
        return True
    g = await s.scalar(select(Group).where(Group.tg_id == tg_id))
    return g is not None and g.status == "joined"


PER_PASS = 20          # запросов за один проход (больше — Telegram начнёт притормаживать поиск)
OFFSET_KEY = "search_offset"
LAST_KEY = "search_last"  # итог последнего поиска — виден в 🔍 Группы и «🧪 Самопроверке»
_PLACES = ("{c}", "объявления {c}", "барахолка {c}", "услуги {c}", "работа {c}", "бизнес {c}", "мастера {c}")


async def build_queries(s) -> list[str]:
    """Свои запросы админа + автоматические: города × «объявления/барахолка/услуги…» и главное слово каждой ниши.
    Раньше были только ~10 ручных запросов — Telegram на них отдаёт одни и те же группы, и через пару дней
    поиск переставал находить новые."""
    from app.models import Category, Keyword, User
    from app.services import geo

    manual = [q.text for q in (await s.scalars(select(SearchQuery).where(SearchQuery.enabled))).all()]
    cities = list(geo.MENU_CITIES)
    for c in (await s.scalars(select(User.city).where(User.city != "").distinct().limit(20))).all():
        if c not in cities:
            cities.append(c)
    auto = [p.format(c=c) for c in cities[:15] for p in _PLACES]
    roots = (await s.scalars(select(Category).where(Category.parent_id.is_(None), Category.enabled,
                                                    ~Category.pending).order_by(Category.id))).all()
    for cat in roots:
        word = await s.scalar(select(Keyword.word).where(Keyword.category_id == cat.id, Keyword.kind == "category")
                              .order_by(Keyword.id))
        if word:
            auto.append(word)
            auto.append(f"{word} {cities[0]}")
    out, seen = [], set()
    for q in manual + auto:
        k = q.strip().casefold()
        if k and k not in seen:
            seen.add(k)
            out.append(q.strip())
    return out


async def next_batch(s) -> tuple[list[str], int, int]:
    """Следующие PER_PASS запросов по кругу: (запросы, с какого номера, всего)."""
    from app.models import Setting

    queries = await build_queries(s)
    if not queries:
        return [], 0, 0
    row = await s.get(Setting, OFFSET_KEY)
    start = int(row.value) % len(queries) if row and row.value.isdigit() else 0
    batch = [queries[(start + i) % len(queries)] for i in range(min(PER_PASS, len(queries)))]
    value = str((start + len(batch)) % len(queries))
    if row:
        row.value = value
    else:
        s.add(Setting(key=OFFSET_KEY, value=value))
    await s.commit()
    return batch, start, len(queries)


async def _options(s) -> dict:
    return {
        "min_members": await settings.get_int(s, "min_members"),
        "min_msgs": await settings.get_int(s, "min_msgs_per_day"),
        "auto": await settings.get(s, "mode") == "auto",
        "check": await settings.get(s, "join_check") == "1",
    }


async def _run_query(client: TelegramClient, q: str, opt: dict, owner=None) -> dict | None:
    """Один поисковый запрос: новые группы — в базу. Счётчики или None, если связь с Telegram потеряна.

    owner — запрос пользователя («🔎 Найти группы для меня»): новые подходящие группы сразу в очередь на вступление
    (проверку перед вступлением они всё равно проходят) и помечаются его, чтобы заявки оттуда он получал первым.
    """
    from app.scout.client import ensure_connected

    c = {"found": 0, "known": 0, "small": 0, "junked": 0, "errors": 0}
    try:
        try:
            res = await client(functions.contacts.SearchRequest(q=q, limit=30))
        except ConnectionError:  # связь оборвалась посреди прохода — переподключиться и повторить запрос
            if not await ensure_connected(client):
                return None
            res = await client(functions.contacts.SearchRequest(q=q, limit=30))
    except FloodWaitError as e:
        log.warning("FloodWait %ss на поиске", e.seconds)
        await asyncio.sleep(e.seconds + 5)
        return c
    except Exception:
        log.exception("поиск «%s»", q)
        c["errors"] += 1
        return c
    for chat in res.chats:
        if _stop_requested:
            break
        if not (isinstance(chat, Channel) and chat.megagroup):
            continue  # нужны группы/чаты, не каналы
        tg_id = utils.get_peer_id(chat)
        async with Session() as s:
            if await s.scalar(select(Group.id).where(Group.tg_id == tg_id)) or await is_covered(s, tg_id):
                c["known"] += 1
                continue
        try:
            full = await client(functions.channels.GetFullChannelRequest(chat))
            members = full.full_chat.participants_count or 0
            msgs_day, texts = await _sample(client, chat)
        except FloodWaitError as e:
            await asyncio.sleep(e.seconds + 5)
            continue
        except Exception:
            log.exception("данные группы %s", chat.title)
            continue
        about = getattr(full.full_chat, "about", "") or ""
        res = groupcheck.by_rules(chat.title or "", about, texts)
        reason = res["reason"] if opt["check"] else ""
        ok = not reason and members >= opt["min_members"] and (msgs_day < 0 or msgs_day >= opt["min_msgs"])
        async with Session() as s:
            g = Group(
                tg_id=tg_id, username=public_username(chat), title=chat.title or "",
                members=members, msgs_per_day=msgs_day, lang=detect_lang(chat.title or ""),
                status=("approved" if opt["auto"] or owner is not None else "candidate") if ok else "skipped",
                skip_reason=reason,
                wanted_by=owner.user_id if owner is not None and ok else 0,
                wanted_search=owner.id if owner is not None and ok else 0,
            )
            groupcheck.apply_place(g, res)
            s.add(g)
            await s.commit()
        if ok:
            c["found"] += 1
        elif reason:
            c["junked"] += 1
        else:
            c["small"] += 1
        await asyncio.sleep(random.uniform(2, 6))
    return c


async def user_pass(client: TelegramClient) -> int:
    """Запросы пользователей («🔎 Найти группы для меня»): новые — сразу, остальные — раз в user_search_days.
    Каждому — сообщение с итогом. Возвращает число новых групп. ConnectionError — связь потеряна."""
    global _stop_requested
    _stop_requested = False  # прошлое «⏹ Остановить поиск» не должно навсегда выключать поиск для пользователей
    from app.models import UserSearch
    from app.scout.client import ensure_connected
    from app.services import mysearch

    async with Session() as s:
        batch = await mysearch.due(s)
        opt = await _options(s)
    if not batch:
        return 0
    if not await ensure_connected(client):
        raise ConnectionError("разведчик без связи")
    total = 0
    for q in batch:
        if _stop_requested:
            break
        c = await _run_query(client, q.text, opt, owner=q)
        if c is None:
            raise ConnectionError("связь оборвалась во время поиска")
        first = q.last_run_at is None
        async with Session() as s:
            row = await s.get(UserSearch, q.id)
            if row is None:  # человек удалил запрос, пока шёл поиск
                continue
            row.last_run_at = now()
            row.found += c["found"]
            await s.commit()
        total += c["found"]
        if first or c["found"]:
            await notify.user_search_result(q.user_id, q.text, c)
        await asyncio.sleep(random.uniform(10, 30))
    return total


async def discover(client: TelegramClient, report: bool = False) -> int:
    """Один проход: сначала запросы пользователей, потом следующая порция общих запросов. Возвращает число новых кандидатов.

    Карточки по одной не шлём — в конце одно сообщение «найдено N», отбор — списком в 🔍 Группы.
    report — поиск запущен кнопкой: сообщить итог, даже если новых нет.
    """
    global _stop_requested
    _stop_requested = False
    from app.scout.client import ensure_connected

    if not await ensure_connected(client):  # нет связи — запросы не тратим, повторим позже
        await save_last("не было связи с Telegram — повторю через 10 мин")
        if report:
            await notify._send("🔍 Поиск не запустился: разведчик сейчас без связи с Telegram. Повторю сам через 10 минут.")
        raise ConnectionError("разведчик без связи")
    await user_pass(client)
    async with Session() as s:
        queries, start, total = await next_batch(s)
        opt = await _options(s)
    auto = opt["auto"]
    found = known = small = errors = junked = 0
    lost = False
    done = 0
    for q in queries:
        done += 1
        if _stop_requested:
            break
        c = await _run_query(client, q, opt)
        if c is None:
            lost = True
            break
        found += c["found"]
        known += c["known"]
        small += c["small"]
        junked += c["junked"]
        errors += c["errors"]
        await asyncio.sleep(random.uniform(10, 30))
    if lost:  # недоделанные запросы вернём в очередь: следующий проход начнёт с них
        await rewind(start + done - 1, total)
    summary = (f"запросов {done - lost} (с {start + 1}-го из {total}), новых {found}, уже знали {known}, "
               f"мелких или тихих {small}" + (f", реклама/флуд {junked}" if junked else "")
               + (f", ошибок {errors}" if errors else "")
               + (" — связь с Telegram оборвалась, продолжу через 10 мин" if lost else ""))
    await save_last(summary)
    if found:
        await notify.groups_found(found, auto)
    if lost:
        if report:
            await notify._send(f"🔍 Поиск прервался: {summary}.")
        raise ConnectionError("связь оборвалась во время поиска")
    if not found and report:
        await notify._send(f"🔍 Поиск закончен: {summary}.\nНовых групп нет — следующий поиск возьмёт другие запросы "
                           "(их можно добавить: 🔍 Группы → 🔎 Поисковые запросы).")
    return found


async def rewind(position: int, total: int) -> None:
    from app.models import Setting

    if total <= 0:
        return
    async with Session() as s:
        row = await s.get(Setting, OFFSET_KEY)
        if row:
            row.value = str(position % total)
            await s.commit()


async def save_last(summary: str) -> None:
    from app.models import Setting, now
    from app.services import quiet

    async with Session() as s:
        row = await s.get(Setting, LAST_KEY)
        value = f"{quiet.to_local(now()):%d.%m %H:%M} — {summary}"
        if row:
            row.value = value
        else:
            s.add(Setting(key=LAST_KEY, value=value))
        await s.commit()


async def last_search() -> str:
    from app.models import Setting

    async with Session() as s:
        row = await s.get(Setting, LAST_KEY)
    return row.value if row else "ещё не было"
