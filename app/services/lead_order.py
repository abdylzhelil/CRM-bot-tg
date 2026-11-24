"""Private saved lead order and stable timestamp/id cursors. No schema changes."""
from sqlalchemy import select, tuple_
from app.models import Delivery, Lead
from app.services import settings

ORDERS = {'desc': 'Сначала свежие', 'asc': 'Сначала старые'}


async def get(s, user_id):
    value = await settings.raw(s, f'user_lead_order_{user_id}', 'desc')
    return value if value in ORDERS else 'desc'


async def put(s, user_id, value):
    if not isinstance(value, str) or value not in ORDERS:
        raise ValueError('Неизвестная сортировка')
    await settings.put(s, f'user_lead_order_{user_id}', value)


def columns(order, reverse=False):
    asc = (order == 'asc') != reverse
    return (Lead.created_at.asc(), Delivery.id.asc()) if asc else (Lead.created_at.desc(), Delivery.id.desc())


def boundary(key, order, after=True, inclusive=False):
    pair = tuple_(Lead.created_at, Delivery.id)
    edge = tuple_(*key)
    greater = (order == 'asc') == after
    return (pair >= edge if inclusive else pair > edge) if greater else (pair <= edge if inclusive else pair < edge)


async def cursor(s, user_id, delivery_id):
    # A changed status must not lose the cursor; foreign deliveries cannot anchor it.
    return (await s.execute(select(Lead.created_at, Delivery.id).join(Delivery, Delivery.lead_id == Lead.id)
                            .where(Delivery.id == delivery_id, Delivery.user_id == user_id))).first()
