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


class AdError(Exception):
    """Понятная причина отказа."""


async def enabled(s: AsyncSession) -> bool:
    return await settings.get(s, "adpost_enabled") == "1"


async def eligible_groups(s: AsyncSession, city: str = "") -> list[Group]:
    """Группы, куда можно заказать рекламу: админ разрешил, разведчик в группе есть, включён и может публиковать."""
    rows = (await s.scalars(
        select(Group).join(ScoutAccount, ScoutAccount.id == Group.account_id)
        .where(Group.status == "joined", Group.kind == "group", Group.ads_allowed, ScoutAccount.enabled, ScoutAccount.can_post)
        .order_by(Group.members.desc()))).all()
    # группы города пользователя — вперёд
    return sorted(rows, key=lambda g: 0 if city and g.city.lower() == city.lower() else 1)


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
    order.price = total_price(await settings.get_int(s, "adpost_price"), len(ids))
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


async def reject(s: AsyncSession, order: AdOrder) -> None:
    if order.status != "pending":
        raise AdError("Заказ уже обработан.")
    order.status, order.decided_at = "rejected", now()
    await s.commit()


async def start(s: AsyncSession, order: AdOrder) -> None:
    """Поставить публикации в очередь (после одобрения/оплаты)."""
    if order.status not in ("pending", "approved"):
        return
    order.status = "running"
    for gid in selected(order):
        s.add(AdPost(order_id=order.id, group_id=gid))
    await s.commit()


async def mark_paid(s: AsyncSession, order_id: int, yk_id: str) -> AdOrder | None:
    """Платёж по заказу прошёл → в очередь. None — заказ не найден или уже оплачен."""
    order = await s.get(AdOrder, order_id)
    if order is None or order.status != "approved":
        return None
    order.yk_id = yk_id
    await start(s, order)
    return order


async def cancel(s: AsyncSession, order: AdOrder) -> int:
    """Отмена: неопубликованные посты снимаются. Возвращает число снятых."""
    if order.status not in ("draft", "pending", "approved", "running"):
        raise AdError("Этот заказ уже нельзя отменить.")
    posts = (await s.scalars(select(AdPost).where(AdPost.order_id == order.id, AdPost.status == "queued"))).all()
    for p in posts:
        p.status = "canceled"
    order.status = "canceled"
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
        Group.account_id == account_id, AdPost.status == "sent", AdPost.sent_at >= start_of_day)) or 0


async def last_sent(s: AsyncSession, account_id: int):
    return await s.scalar(select(func.max(AdPost.sent_at)).join(Group, Group.id == AdPost.group_id).where(
        Group.account_id == account_id, AdPost.status == "sent"))


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
