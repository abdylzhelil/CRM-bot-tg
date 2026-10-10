from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot.common import ask, esc, kb, reply
from app.db import Session
from app.services import settings

router = Router()


@router.message(F.text == "⚙️ Настройки")
async def settings_menu(target: Message | CallbackQuery) -> None:
    rows = []
    async with Session() as s:
        for key, label in settings.LABELS.items():
            rows.append([(f"{label}: {await settings.get(s, key)}", f"st:{key}")])
    rows.append([("👤 Аккаунты-разведчики", "acc:list")])
    await reply(target, "⚙️ Настройки (нажмите, чтобы изменить):", kb(rows))


@router.callback_query(F.data == "st:menu")
async def back(cb: CallbackQuery) -> None:
    await settings_menu(cb)


@router.callback_query(F.data.regexp(r"^st:\w+$"))
async def edit(cb: CallbackQuery, state: FSMContext) -> None:
    key = cb.data[3:]
    if key not in settings.LABELS:
        return await cb.answer()
    hint = " (semi — присылать карточки, auto — вступать самому)" if key == "mode" else " (число)"
    await ask(cb, state, "setting", f"{settings.LABELS[key]}{hint}:", key=key)
