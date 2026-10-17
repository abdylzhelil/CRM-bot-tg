"""Эффекты сообщений Telegram (🎉 и т. п.): работают только в личных чатах, ничего не весят.

Если эффект не принят (старый клиент, группа, старая версия aiogram) — сообщение уходит без него."""
from __future__ import annotations

PARTY = "5046509860389126442"  # 🎉


async def send(reply, text: str, *args, effect: str = PARTY, **kw):
    try:
        return await reply(text, *args, message_effect_id=effect, **kw)
    except Exception:
        return await reply(text, *args, **kw)
