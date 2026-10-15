"""Публикация заказанной рекламы аккаунтами-разведчиками (только в разрешённые группы, с лимитами и паузами)."""
import asyncio
import html
import logging
import random
from datetime import timedelta

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


def post_link(g: Group, message_id: int) -> str:
    return f"https://t.me/{g.username}/{message_id}" if g.username else ""


async def publish_one(manager) -> bool:
    """Опубликовать один пост из очереди. True — попытка была (нужна пауза)."""
    async with Session() as s:
        if not await adposts.enabled(s):
            return False
        chosen = None
        for post in await adposts.due_posts(s):
            g = await s.get(Group, post.group_id)
            acc_id = (g.ad_account_id or g.account_id) if g else None
            acc = await s.get(ScoutAccount, acc_id) if acc_id else None
            client = manager.clients.get(acc.id) if acc else None
            if not g or not g.ads_allowed or acc is None or not acc.enabled or not acc.can_post or client is None:
                if g and not g.ads_allowed:  # админ закрыл группу для рекламы после заказа
                    post.status, post.error = "failed", "группа закрыта для рекламы"
                continue
            if not await adposts.account_ready(s, acc.id):
                continue
            chosen = (post, g, acc, client)
            break
        if chosen is None:
            await s.commit()
            return False
        post, g, acc, client = chosen
        order = await s.get(AdOrder, post.order_id)
        text = await adposts.compose(s, order)
        problem = None
        try:
            entity = await client.get_entity(g.username or g.tg_id)
            msg = await client.send_message(entity, text, link_preview=False)
            post.status, post.sent_at, post.message_id = "sent", now(), msg.id
            post.link = post_link(g, msg.id)
        except (FloodWaitError, SlowModeWaitError) as e:
            post.scheduled_at = now() + timedelta(seconds=e.seconds + random.randint(30, 300))
            log.warning("реклама: ожидание %s с в %s", e.seconds, g.title)
        except PeerFloodError:
            acc.status = "limited"
            post.scheduled_at = now() + timedelta(days=1)
            problem = f"Telegram ограничил аккаунт «{acc.name}» за спам (PeerFlood). Публикации с него отложены на сутки."
        except PERMANENT as e:
            post.status, post.error = "failed", REASON.get(type(e).__name__, type(e).__name__)
        except Exception as e:
            log.exception("реклама: не удалось опубликовать в %s", g.title)
            post.status, post.error = "failed", type(e).__name__
        await s.commit()
        finished = post.status != "queued" and await adposts.finish_if_done(s, order)
        order_id, status, title, error = order.id, post.status, g.title, post.error
    if problem:
        await notify.alert(problem)
    if status == "failed":
        await notify._send(f"⚠️ Реклама, заказ №{order_id}: не вышло в «{html.escape(title)}» ({html.escape(error)}). "
                           "Повторить: 🔍 Группы → 📣 Заказы рекламы.")
    if finished:
        await notify_done(order_id)
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
