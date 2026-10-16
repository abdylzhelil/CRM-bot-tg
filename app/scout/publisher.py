"""Публикация заказанной рекламы аккаунтами-разведчиками (только в разрешённые группы, с лимитами и паузами)."""
import asyncio
import html
import logging
import random
from datetime import timedelta

from sqlalchemy import select, update
from telethon.errors import (
    ChannelPrivateError,
    ChatAdminRequiredError,
    ChatRestrictedError,
    ChatSendMediaForbiddenError,
    ChatSendPhotosForbiddenError,
    ChatSendPlainForbiddenError,
    ChatWriteForbiddenError,
    FloodWaitError,
    ForbiddenError,
    PeerFloodError,
    SlowModeWaitError,
    UserBannedInChannelError,
)

from app.bot import notify
from app.db import Session
from app.models import AdOrder, AdPost, Group, ScoutAccount, now
from app.services import adposts, payments, settings

log = logging.getLogger(__name__)

# причины, после которых повтор в эту группу бессмысленен (группа сама выключается из рекламы)
PERMANENT = (UserBannedInChannelError, ChatAdminRequiredError, ChannelPrivateError)
# группа закрыта для сообщений сейчас (ночной режим, «писать нельзя до…», ограничение новичков) — пробуем позже, до суток
CLOSED = (ChatWriteForbiddenError, ChatRestrictedError, ChatSendPlainForbiddenError)
REASON = {
    "UserBannedInChannelError": "аккаунт заблокирован в группе",
    "ChatAdminRequiredError": "писать могут только админы",
    "ChannelPrivateError": "группа закрыта",
}


async def resolve(client, g: Group):
    """Сначала по ID из памяти клиента (без запроса к Telegram), иначе по @username.

    Поиск по имени (ResolveUsername) Telegram сильно ограничивает — не тратим его на каждый пост."""
    try:
        return await client.get_input_entity(g.tg_id)
    except Exception:
        return await client.get_entity(g.username or g.tg_id)


def post_link(g: Group, message_id: int) -> str:
    return f"https://t.me/{g.username}/{message_id}" if g.username else ""


async def exact_link(client, entity, g: Group, message_id: int) -> str:
    """Точная ссылка на сам пост — её выдаёт Telegram (учитывает темы в группе и закрытые группы).
    Не вышло — собираем t.me/группа/номер."""
    from telethon import functions

    try:
        res = await client(functions.channels.ExportMessageLinkRequest(channel=entity, id=message_id))
        link = getattr(res, "link", "")
        if isinstance(link, str) and link.startswith("https://"):
            return link
    except Exception:
        log.info("реклама: Telegram не дал ссылку на пост в %s", g.title)
    return post_link(g, message_id)


async def _after(closed: list[tuple[int, str]], done: set[int]) -> None:
    for order_id, title in closed:
        await notify._send(f"⚠️ Реклама, заказ №{order_id}: не вышло в «{html.escape(title)}» (группа закрыта для рекламы).",
                           order_markup(order_id))
    for order_id in done:
        await notify_done(order_id)


def order_markup(order_id: int, group_id: int | None = None):
    rows = [[notify.InlineKeyboardButton(text="📣 Открыть заказ", callback_data=f"apa:o:{order_id}")]]
    if group_id:
        rows.append([notify.InlineKeyboardButton(text="📢 Карточка группы", callback_data=f"apg:o:{group_id}")])
    return notify.InlineKeyboardMarkup(inline_keyboard=rows)


async def publish_one(manager) -> bool:
    """Опубликовать один пост из очереди. True — попытка была (нужна пауза)."""
    done: set[int] = set()
    async with Session() as s:
        if not await adposts.enabled(s):
            return False
        chosen, closed = None, []
        for post in await adposts.due_posts(s):
            g = await s.get(Group, post.group_id)
            acc_id = (g.ad_account_id or g.account_id) if g else None
            acc = await s.get(ScoutAccount, acc_id) if acc_id else None
            client = manager.clients.get(acc.id) if acc else None
            if g is None or not g.ads_allowed:  # группу удалили или админ закрыл её для рекламы после заказа
                post.status, post.error = "failed", "группа закрыта для рекламы"
                closed.append((post.order_id, g.title if g else "группа удалена"))
                continue
            if acc is None or not acc.enabled or not acc.can_post or client is None:
                waiting = f"«{acc.name}»" if acc else "без аккаунта"
                await notify.alert_once(f"adpost_acc_{acc_id}", f"Реклама ждёт: аккаунт-публикатор {waiting} выключен, "
                                        "без права публикации или не подключён. Проверьте 👤 Аккаунты или смените аккаунт "
                                        "в 📢 Группы для рекламы.", cooldown=6 * 3600)
                continue
            if not adposts.in_hours(g):
                continue  # у группы свои часы («9-21») — подождёт
            if not await adposts.account_ready(s, acc.id):
                continue
            if await adposts.group_sent_today(s, g.id) >= await settings.get_int(s, "adpost_group_daily"):
                continue  # в эту группу сегодня уже вышло достаточно рекламы — подождёт до завтра
            chosen = (post, g, acc, client)
            break
        if closed:
            await s.commit()
            for order_id, _ in closed:
                if await adposts.finish_if_done(s, await s.get(AdOrder, order_id)):
                    done.add(order_id)
        if chosen is None:
            await s.commit()
            await _after(closed, done)
            return False
        post, g, acc, client = chosen
        order = await s.get(AdOrder, post.order_id)
        text = await adposts.compose(s, order)
        problem, switched_off, conditions_done = None, False, []
        try:
            entity = await resolve(client, g)
            # parse_mode=None: текст рекламодателя публикуем как есть (Markdown Telethon испортил бы *, _, [ ])
            pic = adposts.media_path(order.photo) if order.photo else None
            if pic is not None and pic.exists():  # с картинкой: текст идёт подписью к ней
                msg = await client.send_file(entity, str(pic), caption=text, parse_mode=None)
            else:
                msg = await client.send_message(entity, text, link_preview=False, parse_mode=None)
            post.status, post.sent_at, post.message_id = "sent", now(), msg.id
            post.link = await exact_link(client, entity, g, msg.id)
            g.ad_closed_at = None  # раз вышло — группа открыта
        except (FloodWaitError, SlowModeWaitError) as e:
            post.scheduled_at = now() + timedelta(seconds=e.seconds + random.randint(30, 300))
            log.warning("реклама: ожидание %s с в %s", e.seconds, g.title)
        except PeerFloodError:
            acc.status = "limited"
            # откладываем ВСЕ посты этого аккаунта, иначе он продолжит писать и ограничение станет строже
            await s.execute(update(AdPost).where(
                AdPost.status == "queued",
                AdPost.group_id.in_(select(Group.id).where(adposts.publisher_id() == acc.id)),
            ).values(scheduled_at=now() + timedelta(days=1)).execution_options(synchronize_session=False))
            post.scheduled_at = now() + timedelta(days=1)
            problem = f"Telegram ограничил аккаунт «{acc.name}» за спам (PeerFlood). Публикации с него отложены на сутки."
        except CLOSED:
            g.ad_closed_at = g.ad_closed_at or now()  # новым покупателям её пока не предлагаем
            did = [] if g.kind == "channel" else await try_conditions(
                s, manager, post, g, acc.id, client, entity, now() - timedelta(days=3))
            if did:  # например, «писать могут только подписчики канала» — подписались, пробуем через несколько минут
                post.scheduled_at = now() + timedelta(minutes=random.randint(5, 15))
                post.error = "выполнил условия группы — пробую снова"
                conditions_done = did
            elif g.kind == "channel":  # в канал пишут только админы — ждать бесполезно
                post.status, post.error = "failed", "аккаунт не админ канала (нет права публиковать)"
                switched_off = True
            elif not adposts.closed_now(post):
                post.status, post.error = "failed", "в группе нельзя писать уже сутки (закрыта или нужны условия группы)"
                switched_off = True
        except (ChatSendMediaForbiddenError, ChatSendPhotosForbiddenError):
            post.status, post.error = "failed", "в группе запрещены картинки"
            g.no_media = True  # текстовую рекламу сюда можно, с картинкой — больше не предлагаем
        except PERMANENT as e:
            post.status, post.error = "failed", REASON.get(type(e).__name__, type(e).__name__)
            switched_off = True
        except ForbiddenError as e:  # другой запрет Telegram (платные сообщения в группе, только премиум и т. п.)
            post.status, post.error = "failed", f"Telegram запретил писать ({getattr(e, 'message', '') or type(e).__name__})"[:250]
            switched_off = True
        except Exception as e:
            log.exception("реклама: не удалось опубликовать в %s", g.title)
            post.status, post.error = "failed", f"{type(e).__name__}: {getattr(e, 'message', '') or e}"[:250]
        if switched_off:
            g.ads_allowed = False  # не вышло и не выйдет — убираем группу из списка для покупателей
        await s.commit()
        if post.status != "queued" and await adposts.finish_if_done(s, order):
            done.add(order.id)
        order_id, status, title, error, gid = order.id, post.status, g.title, post.error, g.id
    if problem:
        await notify.alert_once(f"peerflood_{acc.id}", problem)
    if conditions_done:
        await tell_conditions(order_id, title, conditions_done)
    if status == "failed":
        off = (" Реклама в этой группе выключена — покупатели её больше не видят; включить можно в карточке группы."
               if switched_off else "")
        await notify._send(f"⚠️ Реклама, заказ №{order_id}: не вышло в «{html.escape(title)}» ({html.escape(error)}).{off} "
                           "В карточке заказа: повторить или «✂️ Выполнен частично» с возвратом за эту группу.",
                           order_markup(order_id, gid))
    await _after(closed, done)
    return True


async def probe(manager, g: Group) -> tuple[str, str, bool, bool]:
    """«🔎 Проверить группу»: может ли аккаунт-публикатор сейчас писать сюда. Ничего не публикует.

    (итог: ok / warn / bad, пояснение, картинки запрещены, закрыта сейчас). bad — реклама здесь не выйдет, warn — с оговоркой."""
    acc_id = g.ad_account_id or g.account_id
    client = manager.clients.get(acc_id) if acc_id else None
    if client is None:
        return "bad", "аккаунт-публикатор не назначен или не подключён", False, False
    try:
        ent = await client.get_entity(await resolve(client, g))
    except (ChannelPrivateError, UserBannedInChannelError):
        return "bad", "группа закрыта или аккаунт из неё удалён", False, False
    except Exception as e:
        return "warn", f"не удалось проверить ({type(e).__name__}) — попробуйте позже", False, False
    if getattr(ent, "left", False):
        return "bad", "аккаунт не состоит в группе", False, False
    rights = getattr(ent, "admin_rights", None)
    creator = bool(getattr(ent, "creator", False))
    if getattr(ent, "broadcast", False):  # канал: пишут только админы с правом постов
        if creator or (rights is not None and getattr(rights, "post_messages", False)):
            return "ok", "аккаунт — админ канала, можно публиковать", False, False
        return "bad", "аккаунт не админ канала с правом публиковать посты", False, False
    if creator or rights is not None:
        return "ok", "аккаунт — админ группы, ограничений нет", False, False
    limits = [r for r in (getattr(ent, "banned_rights", None), getattr(ent, "default_banned_rights", None)) if r is not None]

    def banned(name: str) -> bool:
        return any(getattr(r, name, False) for r in limits)

    if banned("send_messages") or banned("send_plain"):
        return "warn", "сейчас писать нельзя (закрыта на время или новичкам нельзя) — выбрать её нельзя, проверяю каждые 2 ч", False, True
    no_media = banned("send_media") or banned("send_photos")
    notes = (["картинки запрещены — только текст"] if no_media else []) + (["ссылки запрещены"] if banned("embed_links") else [])
    return ("warn", ", ".join(notes), no_media, False) if notes else ("ok", "можно публиковать", False, False)


async def inspect_rules(manager, g: Group, s=None) -> tuple[str, str, list[str]]:
    """Что за группа изнутри: описание и закреплённое сообщение (правила), боты, админы, медленный режим.
    Возвращает (текст для карточки, запрет рекламы в правилах — или пусто, боты-модераторы)."""
    from telethon import functions
    from telethon.tl.types import ChannelParticipantsAdmins, ChannelParticipantsBots

    client = manager.clients.get(g.ad_account_id or g.account_id)
    if client is None:
        return "", "", []
    entity = await resolve(client, g)
    about, pinned, slow = "", "", 0
    try:
        full = (await client(functions.channels.GetFullChannelRequest(entity))).full_chat
        about, slow = full.about or "", full.slowmode_seconds or 0
        if full.pinned_msg_id:
            msg = await client.get_messages(entity, ids=full.pinned_msg_id)
            pinned = (getattr(msg, "message", "") or "") if msg else ""
    except Exception:
        log.info("реклама: не прочитал описание/закреп %s", g.title)
    bots, admins = [], []
    try:
        bots = [u.username for u in await client.get_participants(entity, filter=ChannelParticipantsBots(), limit=30)
                if getattr(u, "username", None)]
        admins = [u.username for u in await client.get_participants(entity, filter=ChannelParticipantsAdmins(), limit=15)
                  if getattr(u, "username", None) and not getattr(u, "bot", False)]
    except Exception:
        log.info("реклама: список ботов/админов %s скрыт", g.title)
    verdict = adposts.rules_verdict(about + "\n" + pinned)
    ai = None
    if s is not None and (about or pinned):  # ИИ читает правила целиком — точнее, чем поиск по словам
        from app.services import brain

        ai = await brain.group_rules(s, about, pinned)
    if ai is not None:
        verdict = {"forbidden": "в правилах группы реклама запрещена", "only_admin": "реклама только через админа группы",
                   "paid": "реклама в группе платная (у админа)"}.get(ai.get("ads"), "")
        if ai.get("hours") and not g.ad_hours:
            from app.services import quiet

            if quiet.parse_hours(ai["hours"].replace(" ", "")):
                g.ad_hours = ai["hours"].replace(" ", "")[:16]
        g.ad_ai_channels = ",".join(c.lstrip("@") for c in ai.get("subscribe_channels", [])[:3])
    mods = adposts.moderation_bots(bots)
    lines = []
    if ai is not None and ai.get("summary"):
        lines.append("🤖 " + ai["summary"][:200])
    if verdict:
        lines.append("📜 " + verdict)
    if about:
        lines.append("Описание: " + " ".join(about.split())[:200])
    if pinned:
        lines.append("Закреп: " + " ".join(pinned.split())[:200])
    if bots:
        lines.append("🤖 Боты: " + ", ".join("@" + b for b in bots[:8]) + (" (модераторы: " + ", ".join("@" + b for b in mods) + ")" if mods else ""))
    if admins:
        lines.append("👮 Админы (договориться о рекламе): " + ", ".join("@" + a for a in admins[:5]))
    if slow:
        lines.append(f"🐢 Медленный режим: одно сообщение в {slow} с")
    return "\n".join(lines)[:1500], verdict, mods


async def check_group(manager, gid: int) -> tuple[str, str]:
    """Проверить группу и запомнить итог в карточке. bad — реклама в группе выключается."""
    async with Session() as s:
        g = await s.get(Group, gid)
        if g is None:
            return "bad", "группа не найдена"
        level, text, no_media, closed = await probe(manager, g)
        if level != "bad" and g.kind != "channel":  # правила и боты группы (в своём канале — не нужно)
            try:
                g.ad_rules, verdict, mods = await inspect_rules(manager, g, s)
            except Exception:
                verdict, mods = "", []
                log.info("реклама: не удалось прочитать правила %s", g.title)
            if g.ad_rules and await settings.get(s, "adpost_fulfill") == "1":  # «подпишитесь на @канал» в правилах — сразу
                from app.scout import conditions

                channels, _ = conditions.parse(g.ad_rules)
                channels |= {c for c in g.ad_ai_channels.split(",") if c}  # что нашёл ИИ
                client = manager.clients.get(g.ad_account_id or g.account_id)
                subscribed = await conditions.subscribe(client, channels) if channels and client else []
                if subscribed:
                    g.ad_rules = (g.ad_rules + "\n🤝 Подписался по правилам группы: "
                                  + ", ".join("@" + c for c in subscribed))[:1500]
            if verdict and not g.ad_agreed:
                level, text = "bad", verdict + " — договоритесь с админом и нажмите «🤝 Договорился с админом»"
            elif mods and level == "ok":
                level, text = "warn", "есть боты-модераторы (" + ", ".join("@" + m for m in mods[:3]) + ") — пост могут удалить, бот это проверит"
        g.ad_check = ({"ok": "✅ ", "warn": "⚠️ ", "bad": "⛔ "}[level] + text)[:160]
        g.ad_checked_at = now()
        g.no_media = no_media or (g.no_media and level != "ok")
        if closed and g.ad_hours and not adposts.in_hours(g):
            pass  # закрыта вне своих часов («9-21») — так и задумано, покупателям её не прячем
        elif closed:
            g.ad_closed_at = g.ad_closed_at or now()
        elif level != "warn" or "не удалось" not in text:  # проверка прошла и группа открыта
            g.ad_closed_at = None
        if level == "bad":
            g.ads_allowed = False
        await s.commit()
    return level, text


async def recheck_groups(manager) -> list[str]:
    """Фоном: проверить ни разу не проверенные группы и перепроверить закрытые (раз в 2 ч).
    Закрыта дольше 48 ч — значит, писать там нельзя совсем: выключаем рекламу. Возвращает названия выключенных."""
    async with Session() as s:
        ids = await adposts.due_rechecks(s)
    off = []
    for gid in ids:
        level, _ = await check_group(manager, gid)
        await asyncio.sleep(2)  # не дёргаем Telegram слишком часто
        async with Session() as s:
            g = await s.get(Group, gid)
            if g is not None and g.ads_allowed and g.ad_closed_at and now() - g.ad_closed_at >= adposts.CLOSED_GIVE_UP:
                g.ads_allowed = False
                off.append((g.id, g.title))
                await s.commit()
            elif g is not None and level == "bad":
                off.append((g.id, g.title))
    if off:  # одним сообщением за проверку
        await notify._send("⛔ Реклама выключена (покупатели их больше не видят): писать туда нельзя — закрыта больше 2 суток, "
                           "нет доступа или правила группы запрещают рекламу. Причина — в карточке группы.\n"
                           + "\n".join(f"• {html.escape(title)}" for _, title in off),
                           notify.InlineKeyboardMarkup(inline_keyboard=[
                               [notify.InlineKeyboardButton(text=f"📢 {title[:30]}", callback_data=f"apg:o:{gid}")]
                               for gid, title in off[:8]]))
    return [t for _, t in off]


async def try_conditions(s, manager, post: AdPost, g: Group, acc_id: int, client, entity, since) -> list[str]:
    """Один раз на пост: выполнить условия группы (подписка, пригласить своих). Возвращает сделанное."""
    from app.scout import conditions

    if post.fulfilled or await settings.get(s, "adpost_fulfill") != "1":
        return []
    post.fulfilled = True
    acc = await s.get(ScoutAccount, acc_id)
    try:
        did = await conditions.fulfill(manager, acc_id, client, entity, acc.tg_user_id if acc else 0, since,
                                       {post.message_id} if post.message_id else set(), g.ad_rules, s)
    except Exception:
        log.exception("реклама: условия группы %s", g.title)
        return []
    return did if any(x.startswith(("подписался", "пригласил")) for x in did) else []


async def tell_conditions(order_id: int, title: str, did: list[str]) -> None:
    await notify._send(f"🤝 Реклама, заказ №{order_id}: группа «{html.escape(title)}» ставила условия — разведчик "
                       + "; ".join(did) + ". Пробую опубликовать снова.", order_markup(order_id))


async def check_posts(manager) -> int:
    """Через 15 минут после публикации: пост на месте? Если его удалили (правила группы, бот-модератор) —
    отмечаем «удалён», выключаем рекламу в этой группе и сообщаем админу и заказчику. Возвращает число удалённых."""
    gone, retried = [], []
    async with Session() as s:
        for post in await adposts.due_checks(s):
            post.checks += 1
            post.checked = post.checks >= len(adposts.CHECKS)
            g = await s.get(Group, post.group_id)
            acc_id = (g.ad_account_id or g.account_id) if g else None
            client = manager.clients.get(acc_id) if acc_id else None
            if g is None or client is None or not post.message_id:
                continue
            try:
                msg = await client.get_messages(await resolve(client, g), ids=post.message_id)
            except Exception:
                log.warning("реклама: не удалось проверить пост %s в %s", post.id, g.title)
                continue
            if msg is not None:
                post.verified_at = now()  # пост на месте
            if msg is None:
                # бот группы мог удалить пост за невыполненное условие («подпишись», «пригласи друзей») — выполним и повторим
                did = await try_conditions(s, manager, post, g, acc_id, client, await resolve(client, g),
                                           post.sent_at - timedelta(minutes=1))
                if did:
                    post.status, post.link, post.checks, post.checked = "queued", "", 0, False
                    post.error = "выполнил условия группы — пробую снова"
                    post.scheduled_at = now() + timedelta(minutes=random.randint(5, 15))
                    order = await s.get(AdOrder, post.order_id)
                    if order is not None and order.status == "done":
                        order.status = "running"
                    retried.append((post.order_id, g.title, did))
                    continue
                post.status, post.error, post.link = "deleted", "удалён в группе (правила или бот группы)", ""
                post.checked = True
                g.ads_allowed = False  # пока не разберётесь с условиями группы — туда больше не публикуем
                order = await s.get(AdOrder, post.order_id)
                gone.append((post.order_id, order.user_id if order else 0, g.id, g.title,
                             order is not None and order.status == "done"))
        await s.commit()
    for order_id, title, did in retried:
        await tell_conditions(order_id, title, did)
    for order_id, user_id, gid, title, finished in gone:
        refunded = (await refund_unpublished(order_id))[0] if finished else 0  # заказ ещё идёт — вернём при завершении
        await notify._send(
            f"🗑 Реклама, заказ №{order_id}: пост в «{html.escape(title)}» удалили через несколько минут — видимо, правила "
            "или бот группы. Реклама в этой группе выключена. Проверьте условия группы (подписка, «я не бот», согласие админа) "
            "и включите снова в карточке группы (там есть «🔎 Проверить группу»).",
            notify.InlineKeyboardMarkup(inline_keyboard=[
                [notify.InlineKeyboardButton(text="📣 Открыть заказ", callback_data=f"apa:o:{order_id}")],
                [notify.InlineKeyboardButton(text="📢 Карточка группы", callback_data=f"apg:o:{gid}")]]))
        if user_id:
            back = (f"За эту группу вернули {refunded} ₽ — деньги придут тем же способом, обычно за 1–3 дня."
                    if refunded else "Администратор уже знает — по этой группе вернём деньги.")
            try:
                await notify._bot.send_message(
                    user_id, f"⚠️ Ваш рекламный пост в «{html.escape(title)}» удалили модераторы группы. " + back)
            except Exception:
                pass
    return len(gone)


async def refund_unpublished(order_id: int) -> tuple[int, str]:
    """Автовозврат за невышедшие группы заказа. (₽ возвращено, текст ошибки). Ошибку видит админ."""
    async with Session() as s:
        order = await s.get(AdOrder, order_id)
        if order is None:
            return 0, ""
        try:
            return await adposts.auto_refund(s, order), ""
        except payments.RefundError as e:
            return 0, str(e)


async def notify_done(order_id: int) -> None:
    """Заказ закончен: автовозврат за невышедшие группы (если включён) и итог заказчику — ссылки и что не вышло."""
    refunded, problem = await refund_unpublished(order_id)
    async with Session() as s:
        order = await s.get(AdOrder, order_id)
        rows = await adposts.posts_of(s, order_id)
        left = await adposts.unpublished_value(s, order)
    lines = []
    for p, g in rows:
        if p.status == "sent":
            lines.append(f"✅ {html.escape(g.title[:40])} — <a href=\"{html.escape(p.link)}\">открыть пост</a>" if p.link
                         else f"✅ {html.escape(g.title[:40])}")
        elif p.status in ("failed", "deleted"):
            lines.append(f"⚠️ {html.escape(g.title[:40])} — {html.escape(p.error)}")
    ok = sum(1 for p, _ in rows if p.status == "sent")
    text = f"📣 Заказ рекламы №{order_id} выполнен: опубликовано {ok} из {len(rows)}.\n\n" + "\n".join(lines)
    if refunded:
        text += (f"\n\n💸 За группы, где пост не вышел, вернули {refunded} ₽ — деньги придут тем же способом, "
                 "обычно за 1–3 дня.")
    elif ok < len(rows) and left:
        text += "\n\nПо тому, что не вышло, администратор вернёт деньги — ничего делать не нужно."
    try:
        await notify._bot.send_message(order.user_id, text, disable_web_page_preview=True)
    except Exception:
        log.warning("не удалось сообщить заказчику %s", order.user_id)
    if refunded:
        await notify._send(f"💸 Заказ №{order_id}: автоматически вернули {refunded} ₽ за невышедшие группы.", order_markup(order_id))
    elif left:
        why = f" Автовозврат не прошёл: {html.escape(problem)}" if problem else ""
        if problem:
            from app.services import brain

            async with Session() as s:
                hint = await brain.explain_error(s, "автовозврат денег за рекламу через ЮKassa", problem)
            why += ("\n" + html.escape(hint)) if hint else ""
        await notify._send(f"⚠️ Заказ №{order_id} закончен, но не всё вышло: к возврату {left} ₽.{why} "
                           "Карточка заказа → «💸 Вернуть за невышедшие»: вернуть кнопкой или, если ЮKassa не даёт, "
                           "вернуть в кабинете ЮKassa и нажать «✔ Вернул сам».", order_markup(order_id))


async def stuck_orders() -> int:
    """Заказы, которые публикуются дольше 3 суток: напомнить админу (раз в сутки), что их можно завершить частично."""
    async with Session() as s:
        rows = (await s.scalars(select(AdOrder).where(
            AdOrder.status == "running", AdOrder.decided_at.is_not(None),
            AdOrder.decided_at < now() - timedelta(days=3)))).all()
        ids = [o.id for o in rows]
    for oid in ids:
        await notify.alert_once(f"adpost_stuck_{oid}", f"Заказ рекламы №{oid} публикуется уже больше 3 суток. "
                                "Откройте его: можно повторить или нажать «✂️ Выполнен частично».", cooldown=86400)
    return len(ids)


async def nudge_unpaid() -> int:
    """Одобренные, но неоплаченные заказы: напомнить через сутки, отменить через трое. Возвращает число сообщений."""
    from app.bot.common import kb

    async with Session() as s:
        remind, expire = await adposts.stale_unpaid(s)
        data = [(o.id, o.user_id, o.price) for o in remind]
        gone = [(o.id, o.user_id) for o in expire]
        for o in remind:
            o.reminded = True
        for o in expire:
            await adposts.cancel(s, o)
        await s.commit()
    for oid, uid, price in data:
        try:
            await notify._bot.send_message(uid, f"⏰ Заказ рекламы №{oid} ждёт оплаты ({price} ₽). Без оплаты он отменится через 2 суток.",
                                           reply_markup=kb([[(f"💳 Оплатить {price} ₽", f"ap:pay:{oid}")]]))
        except Exception:
            pass
    for oid, uid in gone:
        try:
            await notify._bot.send_message(uid, f"🚫 Заказ рекламы №{oid} отменён: не дождались оплаты. Можно оформить новый.")
        except Exception:
            pass
    return len(data) + len(gone)


async def publish_loop(manager) -> None:
    ticks = 0
    while True:
        did = False
        try:
            did = await publish_one(manager)
            await check_posts(manager)
            if ticks % 5 == 0:  # ~раз в 5 минут — по 2 группы, чтобы не дёргать Telegram
                await recheck_groups(manager)
            ticks += 1
            if ticks % 30 == 0:  # примерно раз в 30 минут
                await nudge_unpaid()
                await stuck_orders()
        except Exception:
            log.exception("publish_loop")
        await asyncio.sleep(random.uniform(40, 120) if did else 60)
