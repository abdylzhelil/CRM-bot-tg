"""Общий вход для сообщений из групп (разведчик, бот в группе, бот модерации).

Пайплайн находит заявку, раздача выбирает получателей, отправка сразу шлёт тем, кому можно сейчас.
"""
from app.bot import notify
from app.db import Session
from app.models import Lead
from app.services.distribute import distribute
from app.services.pipeline import Incoming, process


async def handle(msg: Incoming) -> Lead | None:
    async with Session() as s:
        lead = await process(s, msg)
        if lead:
            await distribute(s, lead)
    if lead:
        await notify.flush_due()
    return lead
