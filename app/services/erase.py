"""Удаление данных пользователя по его просьбе (/deleteme).

Удаляются профиль, настройки, заявки, клиенты CRM, тексты заказов рекламы (открытые заказы отменяются). Группы, которые он добавлял, остаются в базе без привязки к нему
(они публичные). Записи об оплатах остаются для учёта (в них только числовой ID, без имени).
"""
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Client, ClientEvent, Delivery, Group, OfferVote, PendingLink, Reminder, SpamVote, User, UserCategory, UserNegative,
)


async def erase_user(s: AsyncSession, user_id: int) -> bool:
    """False — нельзя удалить (администратор) или пользователя нет."""
    user = await s.get(User, user_id)
    if user is None or user.is_admin:
        return False
    client_ids = (await s.scalars(select(Client.id).where(Client.user_id == user_id))).all()
    if client_ids:
        await s.execute(delete(ClientEvent).where(ClientEvent.client_id.in_(client_ids)))
        await s.execute(delete(Reminder).where(Reminder.client_id.in_(client_ids)))
        await s.execute(delete(Client).where(Client.id.in_(client_ids)))
    for table in (Delivery, UserCategory, UserNegative, SpamVote, OfferVote, PendingLink):
        await s.execute(delete(table).where(table.user_id == user_id))
    await s.execute(Group.__table__.update().where(Group.submitted_by == user_id).values(
        submitted_by=0, ad_until=None, ad_pending=False, license_until=None))
    await s.execute(User.__table__.update().where(User.referred_by == user_id).values(referred_by=0))
    # заказы рекламы: открытые отменяем, тексты (в них контакты) стираем; суммы и статусы остаются для учёта
    from app.models import AdOrder, AdPost

    orders = (await s.scalars(select(AdOrder).where(AdOrder.user_id == user_id))).all()
    for o in orders:
        if o.status in ("draft", "pending", "approved", "running"):
            await s.execute(AdPost.__table__.update().where(AdPost.order_id == o.id, AdPost.status == "queued").values(status="canceled"))
            o.status = "canceled"
        o.text, o.reason = "[удалено по просьбе пользователя]", ""
        if o.photo:
            from app.services.adposts import remove_photo

            remove_photo(o.photo)
            o.photo = ""
    await s.delete(user)
    await s.commit()
    return True
