"""Реклама в группах: пользователь заказывает пост, админ одобряет, платит (если цена > 0), разведчики публикуют.

Публикует аккаунт-разведчик, который состоит в группе. Только в группах, где админ разрешил (`groups.ads_allowed`),
с суточным лимитом и паузами на аккаунт. Под постом — подпись `adpost_footer` (пометка «Реклама» и т. п.).
"""
import uuid
from datetime import timedelta
from pathlib import Path

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app import config
from app.models import AdOrder, AdPost, Group, ScoutAccount, now
from app.services import settings
from app.services.distribute import day_start

MAX_GROUPS = 10  # групп в одном заказе
MIN_TEXT, MAX_TEXT = 20, 1000
OPEN_STATUSES = ("pending", "approved", "running")
STATUS = {
    "draft": "✏️ черновик", "pending": "⏳ на проверке", "approved": "💳 ждёт оплаты", "running": "📤 публикуется",
    "done": "✅ готово", "rejected": "❌ отклонён", "canceled": "🚫 отменён",
}
POST_STATUS = {"queued": "⏳ в очереди", "sent": "✅ опубликовано", "failed": "⚠️ не вышло", "canceled": "🚫 отменено",
               "deleted": "🗑 удалён в группе"}
CLOSED_WAIT = timedelta(hours=24)  # группа закрыта (ночной режим, «писать нельзя») — пробуем снова до суток
CHECK_AFTER = timedelta(minutes=15)  # первая проверка, что пост не удалили правила/бот группы
CHECKS = (CHECK_AFTER, timedelta(hours=3), timedelta(hours=24))  # пост проверяем трижды: боты удаляют и позже


# основы слов, при которых заказ автоматически не одобряется (идёт к администратору на проверку)
FORBIDDEN_STEMS = (
    "алкогол", "пиво", "водк", "коньяк", "табак", "сигарет", "вейп", "кальян", "казино", "ставк", "букмекер", "лотере",
    "займ", "микрокред", "кредит под", "18+", "эскорт", "знакомств", "наркот", "оружи", "пирамид", "быстрый заработок",
    "гадани", "порч", "магия", "экстрасенс", "астролог", "криптосхем", "свинин", "politic", "экстремизм",
)


def suspicious(text: str) -> list[str]:
    low = text.lower()
    return [w for w in FORBIDDEN_STEMS if w in low]


class AdError(Exception):
    """Понятная причина отказа."""


async def enabled(s: AsyncSession) -> bool:
    return await settings.get(s, "adpost_enabled") == "1"


def publisher_id():
    """Аккаунт, который публикует: назначенный вручную (каналы) или тот, что состоит в группе."""
    return func.coalesce(Group.ad_account_id, Group.account_id)


async def eligible_groups(s: AsyncSession, city: str = "", photo: bool = False) -> list[Group]:
    """Группы и каналы, куда можно заказать рекламу: админ разрешил, есть включённый аккаунт-публикатор.

    Группа, где публикация не получилась (бан, закрыта, пост удалили), выключается сама и сюда не попадает;
    для рекламы с картинкой не показываем группы, где картинки запрещены."""
    q = (select(Group).join(ScoutAccount, ScoutAccount.id == publisher_id())
         .where(Group.status == "joined", Group.ads_allowed, ScoutAccount.enabled, ScoutAccount.can_post,
                Group.ad_closed_at.is_(None)))  # закрытую сейчас группу выбрать нельзя (покажем с 🔒)
    if photo:
        q = q.where(~Group.no_media)
    rows = (await s.scalars(q.order_by(Group.members.desc()))).all()
    # группы города пользователя — вперёд
    return sorted(rows, key=lambda g: 0 if city and g.city.lower() == city.lower() else 1)


CLOSED_GIVE_UP = timedelta(hours=48)  # закрыта дольше — значит, навсегда: выключаем рекламу в группе
RECHECK_EVERY = timedelta(hours=2)


async def closed_groups(s: AsyncSession) -> list[Group]:
    """Разрешённые для рекламы группы, которые сейчас закрыты для сообщений (покупателю — с 🔒, выбрать нельзя)."""
    return list((await s.scalars(select(Group).where(
        Group.status == "joined", Group.ads_allowed, Group.ad_closed_at.is_not(None)).order_by(Group.members.desc()))).all())


async def due_rechecks(s: AsyncSession, limit: int = 2) -> list[int]:
    """Какие группы пора (пере)проверить: ни разу не проверенные и закрытые, проверенные больше 2 ч назад."""
    rows = (await s.scalars(select(Group.id).where(
        Group.status == "joined", Group.ads_allowed,
        Group.ad_checked_at.is_(None) | (Group.ad_closed_at.is_not(None) & (Group.ad_checked_at < now() - RECHECK_EVERY)))
        .order_by(Group.ad_checked_at.is_not(None), Group.ad_checked_at).limit(limit))).all()
    return list(rows)


def group_price(g: Group, default: int) -> int:
    return g.ad_price if g.ad_price > 0 else default


async def price_of_groups(s: AsyncSession, ids: list[int]) -> int:
    default = await settings.get_int(s, "adpost_price")
    groups = (await s.scalars(select(Group).where(Group.id.in_(ids)))).all() if ids else []
    return sum(group_price(g, default) for g in groups)


def selected(order: AdOrder) -> list[int]:
    return [int(x) for x in order.group_ids.split(",") if x]


def total_price(per_group: int, n: int) -> int:
    return per_group * n


MAX_CAPTION_TEXT = 800  # подпись к картинке в Telegram — до 1024 знаков вместе с пометкой «Реклама» (до 200)


def media_path(name: str) -> Path:
    return Path(config.MEDIA_DIR) / name


def new_photo_name() -> str:
    """Имя файла для новой картинки (папку создаём здесь же)."""
    Path(config.MEDIA_DIR).mkdir(parents=True, exist_ok=True)
    return f"{uuid.uuid4().hex}.jpg"


def remove_photo(name: str) -> None:
    if name:
        media_path(name).unlink(missing_ok=True)


async def new_draft(s: AsyncSession, user_id: int, text: str, photo: str = "") -> AdOrder:
    """Черновик заказа: текст (или подпись к картинке) и, если есть, файл картинки."""
    if not await enabled(s):
        raise AdError("Реклама в группах сейчас не подключена.")
    text = text.strip()
    limit = MAX_CAPTION_TEXT if photo else MAX_TEXT
    if not MIN_TEXT <= len(text) <= limit:
        if photo and not text:
            raise AdError("Добавьте к картинке подпись — текст рекламы (что предлагаете, цена, как связаться).")
        raise AdError(f"Текст рекламы — от {MIN_TEXT} до {limit} знаков (сейчас {len(text)})."
                      + (" С картинкой Telegram разрешает подпись не длиннее." if photo else ""))
    # брошенные черновики (текст ввели, группы не выбрали) не видны в «Мои заказы» — новый заказ их заменяет
    old = (await s.scalars(select(AdOrder).where(AdOrder.user_id == user_id, AdOrder.status == "draft"))).all()
    for o in old:
        remove_photo(o.photo)
        await s.delete(o)
    open_n = await s.scalar(select(func.count()).select_from(AdOrder).where(
        AdOrder.user_id == user_id, AdOrder.status.in_(OPEN_STATUSES)))
    if (open_n or 0) >= 3:
        raise AdError("У вас уже 3 незавершённых заказа. Дождитесь их или отмените лишние.")
    order = AdOrder(user_id=user_id, text=text, photo=photo)
    s.add(order)
    await s.commit()
    return order


async def _claim(s: AsyncSession, order: AdOrder, before: tuple[str, ...], after: str) -> bool:
    """Сменить статус, только если он всё ещё один из before (одной командой в базе).

    Защита от двойного нажатия и гонок «оплата пришла ↔ заказ отменили»: второй запрос получит False."""
    res = await s.execute(update(AdOrder).where(AdOrder.id == order.id, AdOrder.status.in_(before))
                          .values(status=after).execution_options(synchronize_session=False))
    if res.rowcount != 1:
        await s.refresh(order)
        return False
    order.status = after
    return True


async def toggle_group(s: AsyncSession, order: AdOrder, group_id: int) -> None:
    ids = selected(order)
    if group_id in ids:
        ids.remove(group_id)
    elif len(ids) >= MAX_GROUPS:
        raise AdError(f"В одном заказе не больше {MAX_GROUPS} групп.")
    else:
        ids.append(group_id)
    order.group_ids = ",".join(map(str, ids))
    await s.commit()


async def submit(s: AsyncSession, order: AdOrder) -> AdOrder:
    """Отправить заказ на проверку админу. Цена фиксируется сейчас."""
    if order.status != "draft":
        raise AdError("Заказ уже отправлен.")
    ids = selected(order)
    if not ids:
        raise AdError("Выберите хотя бы одну группу.")
    allowed = {g.id for g in await eligible_groups(s, photo=bool(order.photo))}
    if not set(ids) <= allowed:
        raise AdError("Некоторые группы больше недоступны для рекламы — выберите заново.")
    order.price = await price_of_groups(s, ids)
    order.status = "pending"
    await s.commit()
    return order


async def approve(s: AsyncSession, order: AdOrder) -> str:
    """Одобрил админ. Бесплатный заказ сразу идёт в очередь, платный ждёт оплаты. Возвращает новый статус."""
    if order.status != "pending":
        raise AdError("Заказ уже обработан.")
    if order.price > 0:
        if not await _claim(s, order, ("pending",), "approved"):
            raise AdError("Заказ уже обработан.")
        order.decided_at = now()
        await s.commit()
    else:
        order.decided_at = now()
        if not await start(s, order):
            raise AdError("Заказ уже обработан.")
    return order.status


async def maybe_auto_approve(s: AsyncSession, order: AdOrder) -> str | None:
    """Автоодобрение (если включено): чистый заказ идёт дальше без админа. Возвращает новый статус или None."""
    if order.status != "pending" or await settings.get(s, "adpost_auto_approve") != "1" or suspicious(order.text):
        return None
    return await approve(s, order)


async def stale_unpaid(s: AsyncSession) -> tuple[list[AdOrder], list[AdOrder]]:
    """Заказы, ждущие оплату: (напомнить через 24 ч, отменить через 72 ч). Отмену выполняет вызывающий."""
    orders = (await s.scalars(select(AdOrder).where(AdOrder.status == "approved", AdOrder.decided_at.is_not(None)))).all()
    remind = [o for o in orders if not o.reminded and now() - o.decided_at >= timedelta(hours=24)
              and now() - o.decided_at < timedelta(hours=72)]
    expire = [o for o in orders if now() - o.decided_at >= timedelta(hours=72)]
    return remind, expire


async def reject(s: AsyncSession, order: AdOrder, reason: str = "") -> None:
    if order.status != "pending" or not await _claim(s, order, ("pending",), "rejected"):
        raise AdError("Заказ уже обработан.")
    order.decided_at, order.reason = now(), reason[:300]
    await s.commit()


async def start(s: AsyncSession, order: AdOrder) -> bool:
    """Поставить публикации в очередь (после одобрения/оплаты). False — заказ уже запущен/отменён кем-то другим."""
    if not await _claim(s, order, ("pending", "approved"), "running"):
        return False
    s.add_all(await _new_posts(s, order))
    await s.commit()
    return True


async def _new_posts(s: AsyncSession, order: AdOrder) -> list[AdPost]:
    """Посты заказа; каждому — его доля цены заказа (по ценам групп), чтобы можно было вернуть деньги за невышедшие."""
    ids = selected(order)
    default = await settings.get_int(s, "adpost_price")
    groups = {g.id: g for g in (await s.scalars(select(Group).where(Group.id.in_(ids)))).all()} if ids else {}
    weights = [group_price(groups[i], default) if i in groups else 0 for i in ids]
    prices = split_price(order.price, weights)
    return [AdPost(order_id=order.id, group_id=gid, price=p) for gid, p in zip(ids, prices)]


def split_price(total: int, weights: list[int]) -> list[int]:
    """Разделить сумму заказа по группам пропорционально их ценам (поровну, если цены нулевые); сумма сходится точно."""
    if not weights:
        return []
    if sum(weights) <= 0:
        weights = [1] * len(weights)
    parts = [total * w // sum(weights) for w in weights]
    parts[parts.index(max(parts))] += total - sum(parts)  # остаток от округления — самой дорогой группе
    return parts


async def mark_paid(s: AsyncSession, order_id: int, yk_id: str, amount_kop: int = 0, currency: str = "RUB"):
    """Платёж по заказу прошёл → записываем оплату (выручка, напоминание про чек, возврат кнопкой) и ставим в очередь.

    Возвращает (заказ, платёж) или None — заказ не найден или уже оплачен."""
    from app.models import Payment

    order = await s.get(AdOrder, order_id)
    if order is None or order.status != "approved" or not await _claim(s, order, ("approved",), "running"):
        return None  # отменён/уже оплачен — вызывающий запишет «лишнюю» оплату для возврата
    order.yk_id = yk_id
    pay = Payment(user_id=order.user_id, days=0, amount=amount_kop or order.price * 100, currency=currency,
                  charge_id=f"yk:{yk_id}", provider_id=yk_id)  # days=0 — это реклама, тариф не трогаем
    s.add(pay)
    s.add_all(await _new_posts(s, order))
    await s.commit()  # оплата, статус и очередь постов — одним сохранением
    return order, pay


async def set_order_price(s: AsyncSession, order: AdOrder, price: int) -> None:
    """Админ меняет итоговую цену заказа (пока он на проверке)."""
    if order.status != "pending":
        raise AdError("Цену можно менять только пока заказ на проверке.")
    if not 0 <= price <= 1_000_000:
        raise AdError("Цена от 0 до 1 000 000 ₽.")
    order.price = price
    await s.commit()


async def record_extra_payment(s: AsyncSession, order_id: int, user_id: int, yk_id: str, amount_kop: int, currency: str = "RUB"):
    """Деньги пришли, а заказ уже оплачен или отменён (оплатили вторую ссылку, заплатили после автоотмены).

    Платёж не теряем: записываем (для возврата кнопкой и учёта). None — уже записан ранее."""
    from app.models import Payment

    if await s.scalar(select(Payment.id).where(Payment.charge_id == f"yk:{yk_id}")):
        return None
    pay = Payment(user_id=user_id, days=0, amount=amount_kop, currency=currency, charge_id=f"yk:{yk_id}", provider_id=yk_id)
    s.add(pay)
    await s.commit()
    return pay


async def cancel(s: AsyncSession, order: AdOrder, reason: str = "") -> int:
    """Отмена: неопубликованные посты снимаются. Возвращает число снятых."""
    if not await _claim(s, order, ("draft", "pending", "approved", "running"), "canceled"):
        raise AdError("Этот заказ уже нельзя отменить.")
    posts = (await s.scalars(select(AdPost).where(AdPost.order_id == order.id, AdPost.status == "queued"))).all()
    for p in posts:
        p.status = "canceled"
    if reason:
        order.reason = reason[:300]
    await s.commit()
    return len(posts)


async def refunded(s: AsyncSession, order: AdOrder) -> bool:
    from app.models import Payment

    return bool(order.yk_id) and bool(await s.scalar(
        select(Payment.id).where(Payment.charge_id == f"yk:{order.yk_id}", Payment.refunded)))


async def retry_failed(s: AsyncSession, order: AdOrder) -> int:
    if order.status not in ("running", "done"):
        raise AdError("Заказ отменён — повторять нечего.")
    if await refunded(s, order):
        raise AdError("Деньги за этот заказ возвращены — повторять нельзя.")
    posts = (await s.scalars(select(AdPost).where(
        AdPost.order_id == order.id, AdPost.status.in_(("failed", "deleted")), ~AdPost.settled))).all()
    for p in posts:  # по уже возвращённым деньгам не повторяем
        p.status, p.error, p.scheduled_at = "queued", "", now()
        p.first_try_at, p.checked, p.link = None, False, ""
    if posts and order.status == "done":
        order.status = "running"
    await s.commit()
    return len(posts)


async def footer(s: AsyncSession) -> str:
    return (await settings.get(s, "adpost_footer")).strip()


async def compose(s: AsyncSession, order: AdOrder) -> str:
    f = await footer(s)
    return order.text + (f"\n\n{f}" if f else "")


async def sent_today(s: AsyncSession, account_id: int) -> int:
    start_of_day = day_start()  # местные сутки, как у лимита заявок
    return await s.scalar(select(func.count()).select_from(AdPost).join(Group, Group.id == AdPost.group_id).where(
        publisher_id() == account_id, AdPost.status == "sent", AdPost.sent_at >= start_of_day)) or 0


async def group_sent_today(s: AsyncSession, group_id: int) -> int:
    start_of_day = day_start()  # местные сутки, как у лимита заявок
    return await s.scalar(select(func.count()).select_from(AdPost).where(
        AdPost.group_id == group_id, AdPost.status == "sent", AdPost.sent_at >= start_of_day)) or 0


async def last_sent(s: AsyncSession, account_id: int):
    return await s.scalar(select(func.max(AdPost.sent_at)).join(Group, Group.id == AdPost.group_id).where(
        publisher_id() == account_id, AdPost.status == "sent"))


async def account_ready(s: AsyncSession, account_id: int) -> bool:
    """Можно ли этому аккаунту публиковать прямо сейчас: суточный лимит и пауза."""
    if await sent_today(s, account_id) >= await settings.get_int(s, "adpost_daily_limit"):
        return False
    last = await last_sent(s, account_id)
    gap = timedelta(minutes=await settings.get_int(s, "adpost_gap_min"))
    return last is None or now() - last >= gap


def in_hours(g: Group, hour: int | None = None) -> bool:
    """Можно ли публиковать в группе сейчас: у группы могут быть часы «9-21» (пусто — в любое время)."""
    from app.services import quiet

    hours = quiet.parse_hours(g.ad_hours)
    if not hours:
        return True
    return quiet.in_quiet(quiet.local_now().hour if hour is None else hour, hours)


def closed_now(post: AdPost) -> bool:
    """Группа закрыта для записи. True — ещё ждём (пост перенесён), False — сутки прошли, пора сдаваться."""
    import random

    if post.first_try_at is None:
        post.first_try_at = now()
    if now() - post.first_try_at >= CLOSED_WAIT:
        return False
    post.scheduled_at = now() + timedelta(minutes=random.randint(60, 120))
    post.error = "группа сейчас закрыта для сообщений — попробую позже"
    return True


async def due_checks(s: AsyncSession, limit: int = 3) -> list[AdPost]:
    """Опубликованные посты, которые пора проверить (не удалили ли их правила или бот группы): через 15 мин, 3 ч и сутки."""
    rows = (await s.scalars(select(AdPost).where(
        AdPost.status == "sent", ~AdPost.checked, AdPost.sent_at <= now() - CHECK_AFTER,
        AdPost.sent_at > now() - timedelta(days=3)).order_by(AdPost.sent_at))).all()
    due = [p for p in rows if p.checks < len(CHECKS) and now() - p.sent_at >= CHECKS[p.checks]]
    return due[:limit]


async def due_posts(s: AsyncSession) -> list[AdPost]:
    return list((await s.scalars(
        select(AdPost).join(AdOrder, AdOrder.id == AdPost.order_id)
        .where(AdPost.status == "queued", AdPost.scheduled_at <= now(), AdOrder.status == "running")
        .order_by(AdPost.id))).all())


NOT_PUBLISHED = ("queued", "failed", "deleted", "canceled")


async def order_payment(s: AsyncSession, order: AdOrder):
    from app.models import Payment

    return await s.scalar(select(Payment).where(Payment.charge_id == f"yk:{order.yk_id}")) if order.yk_id else None


async def open_posts(s: AsyncSession, order: AdOrder) -> list[AdPost]:
    """Невышедшие (или удалённые) посты, по которым вопрос денег ещё не закрыт. У старых постов без цены — досчитываем."""
    posts = list((await s.scalars(select(AdPost).where(AdPost.order_id == order.id))).all())
    if posts and order.price and all(p.price == 0 for p in posts):  # посты до обновления — делим цену по ценам групп
        by_group = {p.group_id: p.price for p in await _new_posts(s, order)}
        for p in posts:
            p.price = by_group.get(p.group_id, 0)
    return [p for p in posts if p.status in NOT_PUBLISHED and not p.settled]


async def unpublished_value(s: AsyncSession, order: AdOrder) -> int:
    """Сколько ₽ ещё можно вернуть за группы, где пост не вышел (не больше, чем осталось от платежа)."""
    value = sum(p.price for p in await open_posts(s, order))
    if not value or not order.price:
        return 0
    pay = await order_payment(s, order)
    left = (pay.amount - pay.refunded_amount) // 100 if pay is not None and not pay.refunded else 0
    return max(0, min(value, left))


async def settle(s: AsyncSession, order: AdOrder) -> None:
    """Вопрос денег по невышедшим постам закрыт (вернули или решили не возвращать) — повторно не вернём."""
    for p in await open_posts(s, order):
        p.settled = True
    await s.commit()


async def auto_refund(s: AsyncSession, order: AdOrder) -> int:
    """Автовозврат за невышедшие группы (если включён). Возвращает ₽; 0 — нечего/выключено.
    Ошибку ЮKassa (RefundError) пробрасывает — вызывающий сообщит админу."""
    from app.services import payments

    if await settings.get(s, "adpost_auto_refund") != "1":
        return 0
    value = await unpublished_value(s, order)
    pay = await order_payment(s, order)
    if not value or pay is None:
        return 0
    rub = await payments.refund_part(s, pay, value * 100) // 100
    await settle(s, order)
    return rub


async def estimate(s: AsyncSession, group_ids: list[int]) -> str:
    """Примерно сколько займёт публикация: посты идут по очереди с паузой и суточным лимитом на аккаунт."""
    import math

    groups = (await s.scalars(select(Group).where(Group.id.in_(group_ids)))).all() if group_ids else []
    per_acc: dict[int, int] = {}
    for g in groups:
        acc = g.ad_account_id or g.account_id or 0
        per_acc[acc] = per_acc.get(acc, 0) + 1
    if not per_acc:
        return ""
    most = max(per_acc.values())
    limit = max(await settings.get_int(s, "adpost_daily_limit"), 1)
    gap = await settings.get_int(s, "adpost_gap_min")
    if most > limit:
        return f"примерно {math.ceil(most / limit)} дн. (не больше {limit} постов в сутки с одного аккаунта)"
    hours = math.ceil((most - 1) * gap / 60) if most > 1 else 0
    return "в течение часа" if hours <= 1 else f"примерно за {hours} ч"


async def finish_partial(s: AsyncSession, order: AdOrder) -> int:
    """✂️ Выполнен частично: оставшиеся в очереди посты снимаются, заказ завершается. Возвращает число снятых."""
    if order.status == "running" and not await _claim(s, order, ("running",), "done"):
        raise AdError("Заказ уже изменился — откройте его снова.")
    if order.status != "done":
        raise AdError("Частично завершить можно только заказ, который публикуется или выполнен.")
    posts = (await s.scalars(select(AdPost).where(AdPost.order_id == order.id, AdPost.status == "queued"))).all()
    for p in posts:
        p.status, p.error = "canceled", "заказ завершён частично"
    await s.commit()
    return len(posts)


async def finish_if_done(s: AsyncSession, order: AdOrder) -> bool:
    """Все публикации обработаны → заказ готов. True — только что завершился."""
    left = await s.scalar(select(func.count()).select_from(AdPost).where(AdPost.order_id == order.id, AdPost.status == "queued"))
    if left or order.status != "running":
        return False
    order.status = "done"
    await s.commit()
    return True


async def posts_of(s: AsyncSession, order_id: int) -> list[tuple[AdPost, Group]]:
    rows = await s.execute(select(AdPost, Group).join(Group, Group.id == AdPost.group_id)
                           .where(AdPost.order_id == order_id).order_by(AdPost.id))
    return [(p, g) for p, g in rows.all()]


# ---- массовые действия администратора над группами и каналами
async def candidates(s: AsyncSession) -> list[Group]:
    """Все группы и каналы, где разведчик состоит и есть публичная ссылка: с ними работают массовые кнопки."""
    return list((await s.scalars(select(Group).where(Group.status == "joined", Group.username != "")
                                 .order_by(Group.members.desc()))).all())


def scaled_prices(groups: list[Group], low: int, high: int, step: int = 10) -> dict[int, int]:
    """Цена по размеру: самая маленькая группа — `low`, самая большая — `high`, остальные пропорционально участникам."""
    if not groups:
        return {}
    mn, mx = min(g.members for g in groups), max(g.members for g in groups)
    out = {}
    for g in groups:
        share = 0.0 if mx == mn else (g.members - mn) / (mx - mn)
        price = round((low + (high - low) * share) / step) * step
        out[g.id] = min(max(price, low), high)
    return out


async def set_all_prices(s: AsyncSession, price: int) -> int:
    groups = await candidates(s)
    for g in groups:
        g.ad_price = price
    await s.commit()
    return len(groups)


async def apply_scaled(s: AsyncSession, low: int, high: int) -> int:
    groups = await candidates(s)
    prices = scaled_prices(groups, low, high)
    for g in groups:
        g.ad_price = prices[g.id]
    await s.commit()
    return len(groups)


async def allow_all(s: AsyncSession, flag: bool) -> tuple[int, int]:
    """Разрешить/запретить рекламу везде. Возвращает (изменено, пропущено: нет аккаунта-публикатора)."""
    changed = skipped = 0
    for g in await candidates(s):
        if flag and not (g.ad_account_id or g.account_id):
            skipped += 1
            continue
        if g.ads_allowed != flag:
            g.ads_allowed = flag
        changed += 1
    await s.commit()
    return changed, skipped


def rubles(text: str) -> int | None:
    """Сумма в рублях от админа: только обычные цифры, до 1 000 000. Иначе None."""
    import re

    text = text.strip()
    return int(text) if re.fullmatch(r"[0-9]{1,7}", text) and int(text) <= 1_000_000 else None


def parse_range(text: str) -> tuple[int, int] | None:
    """«300-2000», «300 2000», «300–2000» → (300, 2000)."""
    import re

    nums = re.findall(r"[0-9]+", text)
    if len(nums) != 2 or any(len(n) > 7 for n in nums):
        return None
    low, high = int(nums[0]), int(nums[1])
    return (low, high) if 0 <= low <= high <= 1_000_000 else None
