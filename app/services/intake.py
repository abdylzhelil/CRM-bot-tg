"""Общий вход для сообщений из групп (разведчик, бот в группе, бот модерации, открытые каналы).

Пайплайн находит заявку, раздача выбирает получателей, отправка сразу шлёт тем, кому можно сейчас.
Если группу добавил пользователь — ему начисляется бонус за заявки из неё.
"""
import logging

from app.bot import notify
from app.db import Session
from app.models import Group, Lead
from app.services import catalog, levels, licenses
from app.services.distribute import distribute
from app.services.pipeline import Incoming, process

log = logging.getLogger(__name__)


async def handle(msg: Incoming) -> Lead | None:
    bonuses, new_level, finder = [], None, 0
    async with Session() as s:
        lead = await process(s, msg)
        if lead:
            await distribute(s, lead)
            bonuses = await catalog.award_bonus(s, lead.group_id)
            group = await s.get(Group, lead.group_id) if lead.group_id else None
            if group is not None and group.submitted_by:
                finder = group.submitted_by
                new_level = await levels.refresh(s, finder)
                await licenses.award(s, group)
    if lead:
        await notify.flush_due()
    for user_id, delta, kind in bonuses:
        await notify.bonus(user_id, delta, kind)
    if new_level:
        await notify.level_up(finder, new_level)
    return lead
