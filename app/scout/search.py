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
from app.models import FeedChat, Group, SearchQuery
from app.services import settings

log = logging.getLogger(__name__)


def detect_lang(title: str) -> str:
    cyr = sum("а" <= c.lower() <= "я" or c.lower() == "ё" for c in title)
    return "ru" if cyr >= max(1, len(title) // 3) else "other"


async def _activity(client: TelegramClient, entity: Channel) -> float:
    """Сообщений в сутки по последним 100 сообщениям; -1 если прочитать нельзя."""
    try:
        msgs = [m async for m in client.iter_messages(entity, limit=100)]
    except Exception:
        return -1
    if len(msgs) < 2:
        return 0
    span = (msgs[0].date - msgs[-1].date).total_seconds() / 86400
    return len(msgs) / max(span, 1 / 24)


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


async def discover(client: TelegramClient, report: bool = False) -> int:
    """Один проход по следующей порции запросов. Возвращает число новых кандидатов.

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
    async with Session() as s:
        queries, start, total = await next_batch(s)
        min_members = await settings.get_int(s, "min_members")
        min_msgs = await settings.get_int(s, "min_msgs_per_day")
        auto = await settings.get(s, "mode") == "auto"
    found = known = small = errors = 0
    lost = False
    done = 0
    for q in queries:
        done += 1
        if _stop_requested:
            break
        try:
            try:
                res = await client(functions.contacts.SearchRequest(q=q, limit=30))
            except ConnectionError:  # связь оборвалась посреди прохода — переподключиться и повторить запрос
                if not await ensure_connected(client):
                    lost = True
                    break
                res = await client(functions.contacts.SearchRequest(q=q, limit=30))
        except FloodWaitError as e:
            log.warning("FloodWait %ss на поиске", e.seconds)
            await asyncio.sleep(e.seconds + 5)
            continue
        except Exception:
            log.exception("поиск «%s»", q)
            errors += 1
            continue
        for chat in res.chats:
            if _stop_requested:
                break
            if not (isinstance(chat, Channel) and chat.megagroup):
                continue  # нужны группы/чаты, не каналы
            tg_id = utils.get_peer_id(chat)
            async with Session() as s:
                if await s.scalar(select(Group.id).where(Group.tg_id == tg_id)) or await is_covered(s, tg_id):
                    known += 1
                    continue
            try:
                full = await client(functions.channels.GetFullChannelRequest(chat))
                members = full.full_chat.participants_count or 0
                msgs_day = await _activity(client, chat)
            except FloodWaitError as e:
                await asyncio.sleep(e.seconds + 5)
                continue
            except Exception:
                log.exception("данные группы %s", chat.title)
                continue
            ok = members >= min_members and (msgs_day < 0 or msgs_day >= min_msgs)
            async with Session() as s:
                g = Group(
                    tg_id=tg_id, username=chat.username or "", title=chat.title or "",
                    members=members, msgs_per_day=msgs_day, lang=detect_lang(chat.title or ""),
                    status=("approved" if auto else "candidate") if ok else "skipped",
                )
                s.add(g)
                await s.commit()
            if ok:
                found += 1
            else:
                small += 1
            await asyncio.sleep(random.uniform(2, 6))
        await asyncio.sleep(random.uniform(10, 30))
    if lost:  # недоделанные запросы вернём в очередь: следующий проход начнёт с них
        await rewind(start + done - 1, total)
    summary = (f"запросов {done - lost} (с {start + 1}-го из {total}), новых {found}, уже знали {known}, "
               f"мелких или тихих {small}" + (f", ошибок {errors}" if errors else "")
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
