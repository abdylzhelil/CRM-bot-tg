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


HOME_PER_PASS = 5  # из каждых PER_PASS запросов — столько про свой регион (владелец: «от Дагестана дальше»)
HOME_KEY = "search_offset_home"


async def split_queries(s) -> tuple[list[str], list[str]]:
    """(запросы про свой регион, остальные). Свои запросы админа + автоматические: города × «объявления/барахолка/…»
    и главное слово каждой ниши. Раньше были только ~10 ручных запросов — Telegram на них отдаёт одни и те же группы."""
    from app.models import Category, Keyword, User
    from app.services import geo

    home = await settings.get(s, "home_region")
    manual = [q.text for q in (await s.scalars(select(SearchQuery).where(SearchQuery.enabled))).all()]
    home_places = (geo.home_cities(home) + [home]) if home else []
    near = [c for c, r in geo.CITIES.items() if r in geo.NEIGHBORS.get(home, ())]
    cities = []
    for c in home_places + list(geo.MENU_CITIES) + near + list(
            (await s.scalars(select(User.city).where(User.city != "").distinct().limit(20))).all()):
        if c and c not in cities:
            cities.append(c)
    roots = (await s.scalars(select(Category).where(Category.parent_id.is_(None), Category.enabled,
                                                    ~Category.pending).order_by(Category.id))).all()
    words = []
    for cat in roots:
        word = await s.scalar(select(Keyword.word).where(Keyword.category_id == cat.id, Keyword.kind == "category")
                              .order_by(Keyword.id))
        if word:
            words.append(word)
    home_q = [p.format(c=c) for c in home_places for p in _PLACES]
    if home_places:
        home_q += [f"{w} {home_places[0]}" for w in words] + [f"{w} {home}" for w in words]
    rest = manual + [p.format(c=c) for c in cities[:20] if c not in home_places for p in _PLACES] + words
    if not home_places:
        rest += [f"{w} {cities[0]}" for w in words]

    def dedup(items, seen):
        out = []
        for q in items:
            k = q.strip().casefold()
            if k and k not in seen:
                seen.add(k)
                out.append(q.strip())
        return out

    seen: set[str] = set()
    home_q = dedup(home_q, seen)
    return home_q, dedup(rest, seen)


async def build_queries(s) -> list[str]:
    home_q, rest = await split_queries(s)
    return home_q + rest


async def _take(s, key: str, items: list[str], n: int) -> tuple[list[str], int]:
    from app.models import Setting

    if not items or n <= 0:
        return [], 0
    row = await s.get(Setting, key)
    start = int(row.value) % len(items) if row and row.value.isdigit() else 0
    batch = [items[(start + i) % len(items)] for i in range(min(n, len(items)))]
    value = str((start + len(batch)) % len(items))
    if row:
        row.value = value
    else:
        s.add(Setting(key=key, value=value))
    return batch, start


async def next_batch(s) -> tuple[list[str], int, int]:
    """Следующие PER_PASS запросов: HOME_PER_PASS про свой регион (по своему кругу) + остальные по кругу.
    → (запросы, с какого номера в общем круге, сколько в общем круге)."""
    home_q, rest = await split_queries(s)
    home_part, _ = await _take(s, HOME_KEY, home_q, HOME_PER_PASS if rest else PER_PASS)
    rest_part, start = await _take(s, OFFSET_KEY, rest, PER_PASS - len(home_part))
    await s.commit()
    if not home_part and not rest_part:
        return [], 0, 0
    _last_home[0] = len(home_part)
    return home_part + rest_part, start, len(rest)


_last_home = [0]  # сколько запросов «своего региона» в начале последней пачки (для отката при обрыве связи)


async def _options(s) -> dict:
    return {
        "min_members": await settings.get_int(s, "min_members"),
        "min_msgs": await settings.get_int(s, "min_msgs_per_day"),
        "auto": await settings.get(s, "mode") == "auto",
        "check": await settings.get(s, "join_check") == "1",
        "places": geo_places(await settings.get(s, "scout_places")),
        "grow": await settings.get(s, "scout_grow") == "1",
    }


def geo_places(raw: str) -> list[str]:
    from app.services import geo

    return geo.parse_places(raw)


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
        from app.services import geo

        if not reason and opt.get("places") and geo.place_state(res.get("city", ""), res.get("region", ""),
                                                                chat.title or "", opt["places"]) is False:
            reason = "место не в списке (" + ", ".join(opt["places"][:3]) + ("…" if len(opt["places"]) > 3 else "") + ")"
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
    async with Session() as s:
        if await settings.get(s, "scout_grow") != "1":
            return 0  # владелец остановил поиск и вступление (⚙️ «Новые группы»)
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
    async with Session() as s:
        if await settings.get(s, "scout_grow") != "1":
            await save_last("поиск новых групп выключен (⚙️ → «Новые группы»)")
            if report:
                await notify._send("🔍 Поиск новых групп сейчас выключен — разведчик только читает группы, где уже состоит. "
                                   "Включить: ⚙️ Настройки → 🧹 Проверка групп и капча → «Новые группы».")
            return 0
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
        await rewind(start + max(0, done - 1 - _last_home[0]), total)
        await rewind_home(_last_home[0] - min(done - 1, _last_home[0]))
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


async def rewind_home(undone: int) -> None:
    """Обрыв связи на запросах «своего региона» — недоделанные вернуть в их круг."""
    from app.models import Setting

    if undone <= 0:
        return
    async with Session() as s:
        home_q, _ = await split_queries(s)
        row = await s.get(Setting, HOME_KEY)
        if row and home_q and row.value.isdigit():
            row.value = str((int(row.value) - undone) % len(home_q))
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
