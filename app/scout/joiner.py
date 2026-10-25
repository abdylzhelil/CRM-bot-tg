"""Вступление в группы с лимитами, случайными паузами и обработкой FloodWait; ротация."""
import asyncio
import logging
import random
from datetime import date, datetime, timedelta

from sqlalchemy import func, or_, select
from telethon import TelegramClient, functions
from telethon.errors import (
    ChannelsTooMuchError,
    ChannelPrivateError,
    FloodWaitError,
    InviteRequestSentError,
    UserAlreadyParticipantError,
)

from app.bot import notify
from app.db import Session
from app.models import FeedChat, Group, ScoutAccount, Setting, now
from app.services import settings

log = logging.getLogger(__name__)


async def is_covered_by_other(s, g: Group) -> bool:
    other = await s.scalar(select(Group.id).where(Group.tg_id == g.tg_id, Group.id != g.id, Group.status == "joined"))
    return other is not None


async def paused() -> bool:
    async with Session() as s:
        return await settings.get(s, "scout_paused") == "1"


async def _retry_later(s, g: Group) -> None:
    """Заявка не одобрена (или сбой): повторим через join_retry_days, но не больше join_retry_max попыток."""
    g.join_attempts += 1
    if g.join_attempts >= await settings.get_int(s, "join_retry_max"):
        g.status = "skipped"
    else:
        g.status = "requested"
        g.join_retry_at = now() + timedelta(days=await settings.get_int(s, "join_retry_days"))


async def _quality(client: TelegramClient, entity, g: Group, s=None) -> tuple[str, dict]:
    """Почему не стоит вступать ("" — вступаем) и итог проверки (охват группы).
    Сообщения открытой группы читаются без вступления; правила, затем ИИ (если подключён)."""
    from app.scout.search import _sample
    from app.services import groupcheck, junk

    reason = junk.title_reason(g.title)
    if reason:
        return reason, {}
    if s is not None and await settings.get(s, "captcha_skip") == "1":
        from app.scout.captchas import kick_reason

        try:
            reason = kick_reason(await client(functions.channels.GetFullChannelRequest(entity)))
        except Exception:
            reason = ""
        if reason:  # бот-капча выкинет разведчика через минуту — вступление не тратим
            return reason, {}
    try:
        _, texts = await _sample(client, entity)
    except Exception:
        texts = []
    if s is None:
        res = groupcheck.by_rules(g.title, "", texts)
    else:
        res = await groupcheck.assess(s, g.title, "", texts)
        if res["verdict"] == "good" and "вернул админ" != g.skip_reason:
            from app.services import geo

            places = geo.parse_places(await settings.get(s, "scout_places"))
            if places and geo.place_state(res.get("city") or g.city, res.get("region") or g.region, g.title, places) is False:
                return "место не в списке (" + ", ".join(places[:3]) + ("…" if len(places) > 3 else "") + ")", res
    return ("" if res["verdict"] == "good" else res["reason"] or "не подходит"), res


async def join_one(client: TelegramClient, acc_id: int) -> bool:
    """Вступить в одну одобренную группу. True — если попытка была (нужна пауза)."""
    from app.scout.client import ensure_connected

    async with Session() as s:
        if await settings.get(s, "scout_grow") != "1":
            return False  # вступление остановлено владельцем (⚙️ «Новые группы»)
    if not await ensure_connected(client):
        return False  # без связи не пытаемся: иначе группа зря ушла бы в «повторить через N дней»
    joined_entity, flood_wait = None, 0.0
    async with Session() as s:
        acc = await s.get(ScoutAccount, acc_id)
        today = date.today().isoformat()
        if acc.joined_date != today:
            acc.joined_date, acc.joined_today = today, 0
        total = await s.scalar(
            select(func.count()).select_from(Group).where(Group.account_id == acc_id, Group.status == "joined")
        )
        if acc.joined_today >= acc.daily_join_limit or total >= acc.max_groups:
            await s.commit()
            return False
        # IN (NULL, …) в SQL не находит NULL — поэтому явно: ничья группа или уже назначенная этому аккаунту
        mine = or_(Group.account_id.is_(None), Group.account_id == acc_id)
        # сначала группы, найденные по просьбе пользователя («🔎 Найти группы для меня»), потом «от своего региона
        # дальше» (владелец 25.10: Дагестан → соседи → Россия → заграница), внутри — самые большие
        from app.services import geo

        home = await settings.get(s, "home_region")
        queue = (await s.scalars(select(Group).where(Group.status == "approved", Group.kind == "group", mine)
                                 .order_by(Group.wanted_by == 0, Group.members.desc()).limit(300))).all()
        g = min(queue, key=lambda x: (x.wanted_by == 0, geo.priority(x.city, x.region, x.title, home), -(x.members or 0)),
                default=None)
        if not g:  # новых нет — повторяем заявки, которые админы групп ещё не одобрили (срок повтора подошёл)
            g = await s.scalar(select(Group).where(Group.status == "requested", Group.kind == "group", mine,
                                                   Group.join_retry_at <= now()).order_by(Group.join_retry_at))
        if not g:
            await s.commit()
            return False
        if await s.get(FeedChat, g.tg_id) is not None or await is_covered_by_other(s, g):
            g.status = "covered"  # там уже есть наш бот — второй не заходит
            await s.commit()
            return False
        check = await settings.get(s, "join_check") == "1"
        # отпустить подключение к базе на время запросов к Telegram (они бывают долгими: «подождите N секунд»);
        # раньше несколько таких ожиданий занимали все подключения — «QueuePool limit reached» (21.10)
        await s.commit()
        try:
            entity = await client.get_entity(g.username or g.tg_id)
            if g.status == "approved" and g.skip_reason != "вернул админ" and check:
                reason, res = await _quality(client, entity, g, s)
                if res:
                    g.audit_verdict = res.get("verdict", "")
                    if res.get("ai"):  # ИИ уже посмотрел — перепроверка этой группы через месяц
                        g.audit_at = now()
                    from app.services import groupcheck

                    groupcheck.apply_place(g, res)
                if reason.startswith("капча"):  # бот-капча выкинет разведчика — вступить вручную с телефона, группу не теряем
                    g.status, g.skip_reason, g.account_id = "manual", reason[:120], acc_id
                    await s.commit()
                    log.info("в %s — вручную: %s", g.title, reason)
                    from app.scout.captchas import tell_manual

                    await tell_manual(g.id)
                    return True
                if reason:  # реклама/флуд/заработок/не по-русски — вступление не тратим
                    g.status, g.skip_reason = "skipped", reason[:120]
                    await s.commit()
                    log.info("не вступаю в %s: %s", g.title, reason)
                    return True
            await client(functions.channels.JoinChannelRequest(entity))
            g.status, g.account_id, g.joined_at = "joined", acc_id, now()
            acc.joined_today += 1
            joined_entity = entity
        except UserAlreadyParticipantError:
            g.status, g.account_id, g.joined_at = "joined", acc_id, now()
        except FloodWaitError as e:
            log.warning("FloodWait %ss при вступлении — жду", e.seconds)
            flood_wait = e.seconds + random.uniform(5, 30)  # ждём уже без подключения к базе (ниже)
        except ChannelsTooMuchError:
            acc.joined_today = acc.daily_join_limit
            await notify.alert(f"Аккаунт «{acc.name}» достиг лимита групп Telegram. Запустите ротацию.")
        except ChannelPrivateError:
            g.status = "skipped"  # группа закрыта или нас заблокировали — повторять бессмысленно
        except ConnectionError:
            log.warning("вступление в %s: оборвалась связь — попробую позже", g.title)
            await s.commit()
            return False
        except InviteRequestSentError:
            acc.joined_today += 1  # заявка тоже действие в Telegram — считаем в суточный лимит
            await _retry_later(s, g)
        except Exception:
            log.exception("вступление в %s", g.title)
            await _retry_later(s, g)
        await s.commit()
    if flood_wait:
        await asyncio.sleep(flood_wait)
        return True
    if joined_entity is not None:
        from app.scout.folders import mute, sync_folders

        await mute(client, joined_entity)
        try:
            await sync_folders(client, acc_id)
        except Exception:
            log.warning("папки разведчика не обновлены", exc_info=True)
    return True


async def join_loop(manager) -> None:
    """Раз в цикл — по одному вступлению на аккаунт, затем случайная пауза."""
    while True:
        did = False
        try:
            for acc_id, client in list(manager.clients.items()):
                if await paused():
                    break
                try:
                    did |= await join_one(client, acc_id)
                except Exception:
                    log.exception("join_loop #%s", acc_id)
        except Exception:  # например, база занята — не останавливаем задачу навсегда
            log.exception("join_loop")
        await asyncio.sleep(random.uniform(90, 420) if did else 60)


async def rotate(client: TelegramClient, acc_id: int, limit: int = 10) -> int:
    """Выйти из групп без заявок за N дней (освободить место для новых из очереди)."""
    async with Session() as s:
        days = await settings.get_int(s, "rotation_days")
        cutoff = now() - timedelta(days=days)
        stale = (await s.scalars(
            select(Group).where(
                Group.account_id == acc_id, Group.status == "joined", Group.joined_at < cutoff,
                (Group.last_lead_at.is_(None)) | (Group.last_lead_at < cutoff),
                ~Group.ads_allowed,  # в группах, где продаём рекламу, остаёмся, даже если заявок нет
            ).limit(limit)
        )).all()
        left = 0
        for g in stale:
            try:
                await client(functions.channels.LeaveChannelRequest(await client.get_entity(g.tg_id)))
                g.status = "left"
                left += 1
            except FloodWaitError as e:
                await asyncio.sleep(e.seconds + 5)
            except Exception:
                log.exception("выход из %s", g.title)
            await asyncio.sleep(random.uniform(20, 90))
        await s.commit()
    return left


async def search_loop(manager) -> None:
    from app.scout.search import discover

    await asyncio.sleep(180)  # после запуска даём разведчикам спокойно подключиться
    while True:
        hours, offline = 6, False
        try:
            for acc_id, client in list(manager.clients.items()):
                if await paused():
                    break
                try:
                    await discover(client)
                except ConnectionError:
                    offline = True
                    log.warning("search_loop #%s: нет связи с Telegram — повторю через 10 мин", acc_id)
                except Exception:
                    log.exception("search_loop #%s", acc_id)
            async with Session() as s:
                hours = await settings.get_int(s, "search_interval_h")
        except Exception:
            log.exception("search_loop")
        # оборвалась связь — не ждём 6 часов, пробуем снова через 10 минут
        await _wait_or_user_search(manager, 600 if offline else max(hours, 1) * 3600)


TICK = 120  # как часто между проходами проверять новые запросы пользователей («🔎 Найти группы для меня»)


async def _wait_or_user_search(manager, seconds: float) -> None:
    """Ждём до следующего общего поиска, но новый запрос пользователя выполняем через пару минут, а не через 6 часов."""
    from app.scout.search import user_pass
    from app.services import mysearch

    left = seconds
    while left > 0:
        await asyncio.sleep(min(TICK, left))
        left -= TICK
        try:
            if await paused():
                continue
            async with Session() as s:
                if not await mysearch.has_new(s):
                    continue
            for acc_id, client in list(manager.clients.items()):
                try:
                    await user_pass(client)
                    break
                except ConnectionError:
                    log.warning("поиск для пользователя #%s: нет связи", acc_id)
        except Exception:
            log.exception("поиск для пользователя")


ROTATION_KEY = "rotation_at"  # когда последний раз выходили из пустых групп (переживает перезапуск)


async def rotation_due(every=timedelta(days=7)) -> bool:
    async with Session() as s:
        row = await s.get(Setting, ROTATION_KEY)
        if row is not None and datetime.fromisoformat(row.value) > now() - every:
            return False
        if row is None:
            # первый запуск после обновления: отсчёт с этого момента, а не выход из групп сразу
            s.add(Setting(key=ROTATION_KEY, value=now().isoformat()))
            await s.commit()
            return False
        row.value = now().isoformat()
        await s.commit()
        return True


async def rotation_loop(manager) -> None:
    """Раз в 7 дней (по отметке в базе, а не от запуска) — выйти из групп без заявок."""
    while True:
        try:
            if await rotation_due():
                for acc_id, client in list(manager.clients.items()):
                    try:
                        await rotate(client, acc_id)
                    except Exception:
                        log.exception("rotation #%s", acc_id)
        except Exception:
            log.exception("rotation_loop")
        await asyncio.sleep(3600)
