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


# Три круга поиска (владелец 29.10: «аудитория сначала с Дагестана, потом весь Кавказ, потом вся Россия»):
# 0 — свой регион (`home_region`), 1 — соседи (для Кавказа — весь Кавказ, geo.NEIGHBORS), 2 — остальная Россия.
# Этап `search_stage` растёт, когда круг текущего этапа пройден целиком; на каждом этапе доля пачки по кругам:
STAGE_SHARES = {0: (20, 0, 0), 1: (6, 14, 0), 2: (4, 6, 10)}
TIER_KEYS = ("search_offset_home", "search_offset_near", "search_offset")
TIER_NAMES = ("свой регион", "соседи", "Россия")
STAGE_KEY = "search_stage"
HOME_KEY, OFFSET_KEY = TIER_KEYS[0], TIER_KEYS[2]
HOME_PER_PASS = STAGE_SHARES[2][0]


async def tier_places(s) -> tuple[str, list[str], list[str], list[str]]:
    """(свой регион, места своего круга, места соседей, остальные места)."""
    from app.models import User
    from app.services import geo

    home = await settings.get(s, "home_region")
    home_places = (geo.home_cities(home) + [home]) if home else []
    near_regions = list(geo.NEIGHBORS.get(home, ()))
    near_places = [c for c, r in geo.CITIES.items() if r in near_regions] + near_regions
    user_cities = list((await s.scalars(select(User.city).where(User.city != "").distinct().limit(30))).all())
    for c in user_cities:  # города пользователей из своего региона и соседей ищем в своём круге
        city, region = (c, "") if c in geo.CITIES else geo.find(c)
        t_ = geo.tier(city, region, home)
        if t_ == 0 and c not in home_places:
            home_places.append(c)
        elif t_ == 1 and c not in near_places:
            near_places.append(c)
    rest_places = []
    for c in list(geo.MENU_CITIES) + user_cities + list(geo.CITIES):
        if c and c not in home_places and c not in near_places and c not in rest_places:
            rest_places.append(c)
    return home, home_places, near_places, rest_places


async def niche_words(s) -> list[str]:
    from app.models import Category, Keyword

    roots = (await s.scalars(select(Category).where(Category.parent_id.is_(None), Category.enabled,
                                                    ~Category.pending).order_by(Category.id))).all()
    words = []
    for cat in roots:
        word = await s.scalar(select(Keyword.word).where(Keyword.category_id == cat.id, Keyword.kind == "category")
                              .order_by(Keyword.id))
        if word:
            words.append(word)
    return words


async def tier_queries(s) -> list[list[str]]:
    """[свой регион, соседи, остальное]. Свои запросы админа (в «остальное»), города × «объявления/барахолка/…»,
    главное слово каждой ниши с городом и подсказки ИИ (services/queryideas.py) по каждому кругу."""
    from app.services import queryideas

    manual = [q.text for q in (await s.scalars(select(SearchQuery).where(SearchQuery.enabled))).all()]
    home, home_places, near_places, rest_places = await tier_places(s)
    words = await niche_words(s)
    ideas = await queryideas.stored(s)
    home_q = [p.format(c=c) for c in home_places for p in _PLACES] + ideas[0]
    if home_places:
        home_q += [f"{w} {home_places[0]}" for w in words] + [f"{w} {home}" for w in words]
    near_q = [p.format(c=c) for c in near_places for p in _PLACES] + ideas[1]
    rest = manual + [p.format(c=c) for c in rest_places[:30] for p in _PLACES] + words + ideas[2]
    if not home_places and rest_places:
        rest += [f"{w} {rest_places[0]}" for w in words]

    def dedup(items, seen):
        out = []
        for q in items:
            k = q.strip().casefold()
            if k and k not in seen:
                seen.add(k)
                out.append(q.strip())
        return out

    seen: set[str] = set()
    return [dedup(home_q, seen), dedup(near_q, seen), dedup(rest, seen)]


async def split_queries(s) -> tuple[list[str], list[str]]:
    """(свой регион, всё остальное) — для подсказок и проверок."""
    home_q, near_q, rest = await tier_queries(s)
    return home_q, near_q + rest


async def build_queries(s) -> list[str]:
    return [q for part in await tier_queries(s) for q in part]


async def _take(s, key: str, items: list[str], n: int) -> tuple[list[str], int, bool]:
    """Следующие n из круга items. → (пачка, с какого номера, круг пройден до конца этой пачкой)."""
    from app.models import Setting

    if not items or n <= 0:
        return [], 0, False
    row = await s.get(Setting, key)
    start = int(row.value) % len(items) if row and row.value.isdigit() else 0
    batch = [items[(start + i) % len(items)] for i in range(min(n, len(items)))]
    value = str((start + len(batch)) % len(items))
    if row:
        row.value = value
    else:
        s.add(Setting(key=key, value=value))
    return batch, start, start + len(batch) >= len(items)


async def stage(s) -> int:
    raw = await settings.raw(s, STAGE_KEY)
    return min(int(raw), 2) if raw.isdigit() else 0


async def next_batch(s) -> tuple[list[str], int, int]:
    """Следующие PER_PASS запросов по долям этапа. Пустой круг отдаёт свою долю следующему.
    → (запросы, этап, всего запросов во всех кругах)."""
    tiers = await tier_queries(s)
    st = await stage(s)
    shares = list(STAGE_SHARES[st])
    for i in range(3):  # пустые круги (нет своего региона и т. п.) — их доля дальше
        if not tiers[i] and shares[i]:
            j = next((k for k in range(3) if tiers[k] and k != i), None)
            if j is not None:
                shares[j] += shares[i]
            shares[i] = 0
    parts, wrapped = [], [False, False, False]
    for i in range(3):
        batch, _, w = await _take(s, TIER_KEYS[i], tiers[i], shares[i])
        parts.append(batch)
        wrapped[i] = w
    # этап закончен, когда его главный круг пройден целиком (или пуст)
    if st < 2 and (wrapped[st] or not tiers[st]):
        await settings.put(s, STAGE_KEY, str(st + 1))
    await s.commit()
    _last_parts[:] = [len(p) for p in parts]
    return [q for p in parts for q in p], st, sum(len(x) for x in tiers)


_last_parts = [0, 0, 0]  # сколько запросов каждого круга в последней пачке (для отката при обрыве связи)


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
        from app.services import queryideas

        _, hp, np_, rp = await tier_places(s)
        try:
            await queryideas.refresh(s, [hp, np_, rp[:25]], await niche_words(s))
        except Exception:
            log.info("подсказки ИИ для поиска не получены", exc_info=True)
        queries, st, total = await next_batch(s)
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
        await rewind_tiers(done - 1)
    summary = (f"этап «{STAGE_LABELS[st]}», запросов {done - lost} из {total}, новых {found}, уже знали {known}, "
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


STAGE_LABELS = ("сначала свой регион", "свой регион и соседи", "вся Россия")


async def rewind_tiers(completed: int) -> None:
    """Обрыв связи посреди пачки: недоделанные запросы каждого круга вернуть в их круг."""
    from app.models import Setting

    async with Session() as s:
        tiers = await tier_queries(s)
        prefix = 0
        for i, n in enumerate(_last_parts):
            undone = n - min(n, max(0, completed - prefix))
            prefix += n
            row = await s.get(Setting, TIER_KEYS[i])
            if undone > 0 and row and tiers[i] and row.value.isdigit():
                row.value = str((int(row.value) - undone) % len(tiers[i]))
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
