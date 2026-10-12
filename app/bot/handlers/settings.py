from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot.common import ask, esc, kb, reply
from app.db import Session
from app.services import settings

router = Router()


@router.message(F.text == "⚙️ Настройки")
async def settings_menu(target: Message | CallbackQuery) -> None:
    """Главная страница: разделы по два в ряд, чтобы не было длинной простыни."""
    buttons = [(f"{icon} {title}", f"st:sec:{i}") for i, (icon, title, _) in enumerate(settings.SECTIONS)]
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    rows.append([("👤 Аккаунты-разведчики", "acc:list")])
    rows.append([("📢 Статистику в канал", "pub:now")])
    await reply(target, "⚙️ <b>Настройки</b>\nВыберите раздел:", kb(rows))


@router.callback_query(F.data == "st:menu")
async def back(cb: CallbackQuery) -> None:
    await settings_menu(cb)


@router.callback_query(F.data.regexp(r"^st:sec:\d+$"))
async def section(cb: CallbackQuery) -> None:
    icon, title, keys = settings.SECTIONS[int(cb.data.split(":")[2])]
    rows = []
    async with Session() as s:
        for key in keys:
            value = await settings.get(s, key) or "выкл"
            rows.append([(f"{settings.SHORT.get(key, settings.LABELS[key])}: {value}", f"st:{key}")])
    idx = cb.data.split(":")[2]
    rows.append([("◀️ Все настройки", "st:menu")])
    text = f"{icon} <b>{esc(title)}</b>\nНажмите на пункт, чтобы изменить."
    if idx == "0":
        text += "\n\nЦена 0 — пакет не продаётся и кнопка не показывается."
    await reply(cb, text, kb(rows))


@router.callback_query(F.data.regexp(r"^st:\w+$"))
async def edit(cb: CallbackQuery, state: FSMContext) -> None:
    key = cb.data[3:]
    if key not in settings.LABELS:
        return await cb.answer()
    hint = {
        "mode": " (semi — присылать карточки, auto — вступать самому)",
        "quiet_hours": ": с какого по какой час копить заявки, напр. 23-8. "
        "🔥 Горячие придут сразу. «-» — выключить",
        "report_hour": " (0–23, по времени TZ_NAME из .env)",
        "channel_hour": " (0–23, по времени TZ_NAME из .env)",
    }.get(key, " (число)")
    await ask(cb, state, "setting", f"{settings.LABELS[key]}{hint}:", key=key)


@router.callback_query(F.data == "pub:now")
async def publish_now(cb: CallbackQuery) -> None:
    from app.services import public_stats

    ok = await public_stats.publish(cb.bot)
    await cb.answer("Опубликовано в канале" if ok else
                    "Не вышло: задайте STATS_CHANNEL в .env и сделайте бота администратором канала", show_alert=True)
