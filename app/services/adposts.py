"""Реклама в группах: пользователь заказывает пост, админ одобряет, платит (если цена > 0), разведчики публикуют.

Публикует аккаунт-разведчик, который состоит в группе. Только в группах, где админ разрешил (`groups.ads_allowed`),
с суточным лимитом и паузами на аккаунт. Под постом — подпись `adpost_footer` (пометка «Реклама» и т. п.).
"""
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AdOrder, AdPost, Group, ScoutAccount, now
from app.services import settings

MAX_GROUPS = 10  # групп в одном заказе
MIN_TEXT, MAX_TEXT = 20, 1000
OPEN_STATUSES = ("pending", "approved", "running")
STATUS = {
    "draft": "✏️ черновик", "pending": "⏳ на проверке", "approved": "💳 ждёт оплаты", "running": "📤 публикуется",
    "done": "✅ готово", "rejected": "❌ отклонён", "canceled": "🚫 отменён",
}
POST_STATUS = {"queued": "⏳ в очереди", "sent": "✅ опубликовано", "failed": "⚠️ не вышло", "canceled": "🚫 отменено"}


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


async def eligible_groups(s: AsyncSession, city: str = "") -> list[Group]:
    """Группы и каналы, куда можно заказать рекламу: админ разрешил, есть включённый аккаунт-публикатор."""
    rows = (await s.scalars(
        select(Group).join(ScoutAccount, ScoutAccount.id == publisher_id())
        .where(Group.status == "joined", Group.ads_allowed, ScoutAccount.enabled, ScoutAccount.can_post)
        .order_by(Group.members.desc()))).all()
    # группы города пользователя — вперёд
    return sorted(rows, key=lambda g: 0 if city and g.city.lower() == city.lower() else 1)


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


async def new_draft(s: AsyncSession, user_id: int, text: str) -> AdOrder:
    if not await enabled(s):
        raise AdError("Реклама в группах сейчас не подключена.")
    text = text.strip()
    if not MIN_TEXT <= len(text) <= MAX_TEXT:
        raise AdError(f"Текст рекламы — от {MIN_TEXT} до {MAX_TEXT} знаков (сейчас {len(text)}).")
    open_n = await s.scalar(select(func.count()).select_from(AdOrder).where(
        AdOrder.user_id == user_id, AdOrder.status.in_(("draft",) + OPEN_STATUSES)))
    if (open_n or 0) >= 3:
        raise AdError("У вас уже 3 незавершённых заказа. Дождитесь их или отмените лишние.")
    order = AdOrder(user_id=user_id, text=text)
    s.add(order)
    await s.commit()
    return order


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
    allowed = {g.id for g in await eligible_groups(s)}
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
    order.decided_at = now()
    if order.price > 0:
        order.status = "approved"
        await s.commit()
    else:
        await start(s, order)
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
    if order.status != "pending":
        raise AdError("Заказ уже обработан.")
    order.status, order.decided_at, order.reason = "rejected", now(), reason[:300]
    await s.commit()


async def start(s: AsyncSession, order: AdOrder) -> None:
    """Поставить публикации в очередь (после одобрения/оплаты)."""
    if order.status not in ("pending", "approved"):
        return
    order.status = "running"
    for gid in selected(order):
        s.add(AdPost(order_id=order.id, group_id=gid))
    await s.commit()


async def mark_paid(s: AsyncSession, order_id: int, yk_id: str, amount_kop: int = 0, currency: str = "RUB"):
    """Платёж по заказу прошёл → записываем оплату (выручка, напоминание про чек, возврат кнопкой) и ставим в очередь.

    Возвращает (заказ, платёж) или None — заказ не найден или уже оплачен."""
    from app.models import Payment

    order = await s.get(AdOrder, order_id)
    if order is None or order.status != "approved":
        return None
    order.yk_id = yk_id
    pay = Payment(user_id=order.user_id, days=0, amount=amount_kop or order.price * 100, currency=currency,
                  charge_id=f"yk:{yk_id}", provider_id=yk_id)  # days=0 — это реклама, тариф не трогаем
    s.add(pay)
    await start(s, order)
    return order, pay


async def set_order_price(s: AsyncSession, order: AdOrder, price: int) -> None:
    """Админ меняет итоговую цену заказа (пока он на проверке)."""
    if order.status != "pending":
        raise AdError("Цену можно менять только пока заказ на проверке.")
    if not 0 <= price <= 1_000_000:
        raise AdError("Цена от 0 до 1 000 000 ₽.")
    order.price = price
    await s.commit()


async def cancel(s: AsyncSession, order: AdOrder, reason: str = "") -> int:
    """Отмена: неопубликованные посты снимаются. Возвращает число снятых."""
    if order.status not in ("draft", "pending", "approved", "running"):
        raise AdError("Этот заказ уже нельзя отменить.")
    posts = (await s.scalars(select(AdPost).where(AdPost.order_id == order.id, AdPost.status == "queued"))).all()
    for p in posts:
        p.status = "canceled"
    order.status = "canceled"
    if reason:
        order.reason = reason[:300]
    await s.commit()
    return len(posts)


async def retry_failed(s: AsyncSession, order: AdOrder) -> int:
    posts = (await s.scalars(select(AdPost).where(AdPost.order_id == order.id, AdPost.status == "failed"))).all()
    for p in posts:
        p.status, p.error, p.scheduled_at = "queued", "", now()
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
    start_of_day = now().replace(hour=0, minute=0, second=0, microsecond=0)
    return await s.scalar(select(func.count()).select_from(AdPost).join(Group, Group.id == AdPost.group_id).where(
        publisher_id() == account_id, AdPost.status == "sent", AdPost.sent_at >= start_of_day)) or 0


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


async def due_posts(s: AsyncSession) -> list[AdPost]:
    return list((await s.scalars(
        select(AdPost).join(AdOrder, AdOrder.id == AdPost.order_id)
        .where(AdPost.status == "queued", AdPost.scheduled_at <= now(), AdOrder.status == "running")
        .order_by(AdPost.id))).all())


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
