"""Общий вход для сообщений из групп (разведчик, бот в группе, бот модерации, открытые каналы).

Пайплайн находит заявку, раздача выбирает получателей, отправка сразу шлёт тем, кому можно сейчас.
Если группу добавил пользователь — ему начисляется бонус за заявки из неё.
"""
import logging

from app.bot import notify
from app.db import Session
from app.models import Lead
from app.services import catalog
from app.services.distribute import distribute
from app.services.pipeline import Incoming, process

log = logging.getLogger(__name__)


async def handle(msg: Incoming) -> Lead | None:
    bonus = None
    async with Session() as s:
        lead = await process(s, msg)
        if lead:
            await distribute(s, lead)
            bonus = await catalog.award_bonus(s, lead.group_id)
    if lead:
        await notify.flush_due()
    if bonus:
        await notify.bonus(*bonus)
    return lead
