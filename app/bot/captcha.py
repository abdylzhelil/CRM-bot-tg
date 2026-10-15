"""Проверка «я не бот» при первом входе: новый пользователь должен нажать на названный значок.

Останавливает простых скриптов и массовые заходы; пока проверка не пройдена, регистрации нет, заявки не раздаются.
Существующие пользователи считаются проверенными (колонка verified по умолчанию истинна).
"""
import random

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message

from app.bot.common import kb
from app.db import Session
from app.models import User

ICONS = {"🍎": "яблоко", "🚗": "машину", "⭐": "звезду", "🐱": "кота", "🌳": "дерево", "🏠": "дом"}
router = Router()
# id пользователя -> (значки на кнопках, нужный значок, параметр /start для приглашения)
_pending: dict[int, tuple[list[str], str, str]] = {}


def make_challenge(user_id: int, start_param: str = "") -> tuple[str, object]:
    icons = random.sample(list(ICONS), 4)
    target = random.choice(icons)
    _pending[user_id] = (icons, target, start_param or _pending.get(user_id, ([], "", ""))[2])
    text = f"🤖 <b>Проверка</b>\nЧтобы начать, нажмите на {ICONS[target]}: {target}"
    return text, kb([[(i, f"cap:{n}") for n, i in enumerate(icons)]])


def start_param_of(event) -> str:
    text = getattr(event, "text", "") or ""
    parts = text.split(maxsplit=1)
    return parts[1] if len(parts) == 2 and parts[0].startswith("/start") else ""


async def gate(event, handler, data, user: User):
    """Вызывается middleware для непроверенных: пропускает только ответ на проверку, остальное — показывает проверку."""
    if isinstance(event, CallbackQuery) and (event.data or "").startswith("cap:"):
        return await handler(event, data)
    text, markup = make_challenge(user.tg_id, start_param_of(event))
    target = event.message if isinstance(event, CallbackQuery) else event
    await target.answer(text, reply_markup=markup)
    if isinstance(event, CallbackQuery):
        await event.answer()
    return None


@router.callback_query(F.data.regexp(r"^cap:\d$"))
async def answer(cb: CallbackQuery, user: User) -> None:
    from app.bot import intro
    from app.bot.handlers import profile
    from app.services import referrals

    if user.verified:
        return await cb.answer()
    pending = _pending.get(user.tg_id)
    chosen = int(cb.data.split(":")[1])
    if pending is None or chosen >= len(pending[0]) or pending[0][chosen] != pending[1]:
        text, markup = make_challenge(user.tg_id)
        await cb.answer("Не то, попробуйте ещё раз")
        return await cb.message.edit_text(text, reply_markup=markup)
    _, _, param = _pending.pop(user.tg_id)
    async with Session() as s:
        row = await s.get(User, user.tg_id)
        row.verified = True
        await s.commit()
        referrer = referrals.parse_payload(param)
        if referrer:
            await referrals.attach(s, row, referrer)
        await referrals.attach_source(s, row, param)
    user.verified = True
    await cb.answer("✅ Проверка пройдена")
    await cb.message.delete()
    await intro.send_radar(cb.message)
    await profile.show_niches(cb.message, user)
