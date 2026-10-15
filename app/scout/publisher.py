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
    ChatWriteForbiddenError,
    FloodWaitError,
    PeerFloodError,
    SlowModeWaitError,
    UserBannedInChannelError,
)

from app.bot import notify
from app.db import Session
from app.models import AdOrder, AdPost, Group, ScoutAccount, now
from app.services import adposts, settings

log = logging.getLogger(__name__)

# причины, после которых повтор в эту группу бессмысленен
PERMANENT = (ChatWriteForbiddenError, UserBannedInChannelError, ChatAdminRequiredError, ChannelPrivateError)
REASON = {
    "ChatWriteForbiddenError": "в группе запрещено писать",
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


async def _after(closed: list[tuple[int, str]], done: set[int]) -> None:
    for order_id, title in closed:
        await notify._send(f"⚠️ Реклама, заказ №{order_id}: не вышло в «{html.escape(title)}» (группа закрыта для рекламы).",
                           order_markup(order_id))
    for order_id in done:
        await notify_done(order_id)


def order_markup(order_id: int):
    return notify.InlineKeyboardMarkup(inline_keyboard=[[
        notify.InlineKeyboardButton(text="📣 Открыть заказ", callback_data=f"apa:o:{order_id}")]])


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
        problem = None
        try:
            entity = await resolve(client, g)
            # parse_mode=None: текст рекламодателя публикуем как есть (Markdown Telethon испортил бы *, _, [ ])
            pic = adposts.media_path(order.photo) if order.photo else None
            if pic is not None and pic.exists():  # с картинкой: текст идёт подписью к ней
                msg = await client.send_file(entity, str(pic), caption=text, parse_mode=None)
            else:
                msg = await client.send_message(entity, text, link_preview=False, parse_mode=None)
            post.status, post.sent_at, post.message_id = "sent", now(), msg.id
            post.link = post_link(g, msg.id)
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
        except PERMANENT as e:
            post.status, post.error = "failed", REASON.get(type(e).__name__, type(e).__name__)
        except Exception as e:
            log.exception("реклама: не удалось опубликовать в %s", g.title)
            post.status, post.error = "failed", type(e).__name__
        await s.commit()
        if post.status != "queued" and await adposts.finish_if_done(s, order):
            done.add(order.id)
        order_id, status, title, error = order.id, post.status, g.title, post.error
    if problem:
        await notify.alert_once(f"peerflood_{acc.id}", problem)
    if status == "failed":
        await notify._send(f"⚠️ Реклама, заказ №{order_id}: не вышло в «{html.escape(title)}» ({html.escape(error)}). "
                           "Повторить можно в карточке заказа.", order_markup(order_id))
    await _after(closed, done)
    return True


async def notify_done(order_id: int) -> None:
    """Сообщить заказчику итог: ссылки на опубликованное и что не вышло."""
    async with Session() as s:
        order = await s.get(AdOrder, order_id)
        rows = await adposts.posts_of(s, order_id)
    lines = []
    for p, g in rows:
        if p.status == "sent":
            lines.append(f"✅ <a href=\"{p.link}\">{html.escape(g.title[:40])}</a>" if p.link else f"✅ {html.escape(g.title[:40])}")
        elif p.status == "failed":
            lines.append(f"⚠️ {html.escape(g.title[:40])} — {html.escape(p.error)}")
    ok = sum(1 for p, _ in rows if p.status == "sent")
    text = f"📣 Заказ рекламы №{order_id} выполнен: опубликовано {ok} из {len(rows)}.\n\n" + "\n".join(lines)
    if ok < len(rows):
        text += "\n\nПо тому, что не вышло, напишите администратору — решим вопрос с возвратом."
    try:
        await notify._bot.send_message(order.user_id, text, disable_web_page_preview=True)
    except Exception:
        log.warning("не удалось сообщить заказчику %s", order.user_id)


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
            ticks += 1
            if ticks % 30 == 0:  # примерно раз в 30 минут
                await nudge_unpaid()
        except Exception:
            log.exception("publish_loop")
        await asyncio.sleep(random.uniform(40, 120) if did else 60)
