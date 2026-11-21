"""Задачи одного исходного сообщения, сохранённые в обычных записях Lead.

Пайплайн назначает им одно время создания. Автор + хеш исходного текста +
это время отличают источник от более поздней повторной заявки того же автора.
Старые одиночные записи остаются обычными источниками из одной задачи.
"""
from sqlalchemy import and_, select
from sqlalchemy.orm import aliased

from app.models import Lead, Delivery


def key(lead):
    return lead.author_id, lead.text_hash, lead.created_at


def same_source(other, lead=Lead):
    return and_(other.author_id == lead.author_id, other.text_hash == lead.text_hash,
                other.created_at == lead.created_at)


async def tasks(s, lead):
    return list((await s.scalars(select(Lead).where(same_source(Lead, lead)).order_by(Lead.id))).all())


async def delivery(s, user_id, lead):
    return await s.scalar(select(Delivery).join(Lead, Lead.id == Delivery.lead_id).where(
        Delivery.user_id == user_id, same_source(Lead, lead)).order_by(Delivery.id).limit(1))


def in_niches(ids):
    return filter_tasks(ids)


def filter_tasks(ids=None, place=""):
    from app.services import geo

    other = aliased(Lead)
    q = select(other.id).where(same_source(other))
    if ids is not None:
        q = q.where(other.category_id.in_(ids))
    if place:
        if place in geo.REGIONS and place not in geo.CITIES:
            q = q.where((other.region == place) | other.city.in_(geo.home_cities(place)))
        else:
            q = q.where(other.city == place)
    return q.exists()


async def category_label(s, lead):
    from app.bot.handlers.leads import category_name

    rows = await tasks(s, lead)
    names = []
    for task in rows or [lead]:
        name = await category_name(s, task.category_id)
        if len(rows) > 1 and (task.city or task.region):
            name += f" ({task.city or task.region})"
        if name not in names:
            names.append(name)
    return " + ".join(names)
