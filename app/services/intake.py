"""Общий вход для сообщений из групп (разведчик и бот в группе): пайплайн + уведомление."""
from app.bot import notify
from app.db import Session
from app.models import Lead
from app.services.pipeline import Incoming, process


async def handle(msg: Incoming) -> Lead | None:
    from app.bot.handlers.leads import category_name

    async with Session() as s:
        lead = await process(s, msg)
        if lead:
            await notify.deliver_lead(lead, await category_name(s, lead.category_id))
    return lead
