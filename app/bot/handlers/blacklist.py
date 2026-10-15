from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import delete, func, select

from app.bot.common import ask, esc, kb, reply
from app.db import Session
from app.models import BlacklistEntry, SpamVote

router = Router()
PAGE = 10
TITLES = {"spam": "🚫 Чёрный список", "client": "👍 Белый список (это клиенты)"}


def who(e: BlacklistEntry) -> str:
    return f"@{e.username}" if e.username else str(e.user_id)


@router.message(F.text == "🛡 Чёрный список")
async def blacklist_menu(target: Message | CallbackQuery) -> None:
    async with Session() as s:
        cnt = dict((await s.execute(select(BlacklistEntry.status, func.count()).group_by(BlacklistEntry.status))).all())
    await reply(target, "🛡 <b>Списки авторов заявок</b>\n"
                "🚫 Чёрный — их сообщения не становятся заявками.\n👍 Белый — всегда заявки, даже если похожи на спам.",
                kb([[(f"🚫 Чёрный · {cnt.get('spam', 0)}", "bl:ls:spam:0"), (f"👍 Белый · {cnt.get('client', 0)}", "bl:ls:client:0")],
                    [("🔎 Найти по ID или @нику", "bl:find"), ("➕ Добавить по ID", "bl:add")]]))


@router.callback_query(F.data == "bl:menu")
async def back(cb: CallbackQuery) -> None:
    await blacklist_menu(cb)


@router.callback_query(F.data.regexp(r"^bl:ls:(spam|client):\d{1,6}$"))
async def show_list(cb: CallbackQuery) -> None:
    """Все записи страницами по 10, новые сверху; нажатие — карточка с кнопкой «убрать»."""
    _, _, status, raw = cb.data.split(":")
    offset = int(raw)
    async with Session() as s:
        total = await s.scalar(select(func.count()).select_from(BlacklistEntry).where(BlacklistEntry.status == status)) or 0
        rows_ = (await s.scalars(select(BlacklistEntry).where(BlacklistEntry.status == status)
                                 .order_by(BlacklistEntry.created_at.desc()).offset(offset).limit(PAGE))).all()
    rows = [[(f"{who(e)} · {e.reason[:30]}" if e.reason else who(e), f"bl:o:{e.user_id}")] for e in rows_]
    nav = []
    if offset:
        nav.append(("⬅️", f"bl:ls:{status}:{max(offset - PAGE, 0)}"))
    if offset + PAGE < total:
        nav.append(("➡️", f"bl:ls:{status}:{offset + PAGE}"))
    if nav:
        rows.append(nav)
    rows.append([("◀️ Назад", "bl:menu")])
    page = f" (с {offset + 1} по {offset + len(rows_)} из {total})" if total > PAGE else f": {total}"
    await reply(cb, f"{TITLES[status]}{page}" + ("" if rows_ else "\nПусто."), kb(rows))


async def card(target, user_id: int) -> None:
    async with Session() as s:
        e = await s.get(BlacklistEntry, user_id)
        votes = await s.scalar(select(func.count()).select_from(SpamVote).where(SpamVote.author_id == user_id)) or 0
    if e is None:
        return await reply(target, "Этого автора в списках нет.", kb([[("◀️ Назад", "bl:menu")]]))
    text = (f"{TITLES[e.status] if e.status in TITLES else e.status}\n"
            f"Автор: {esc(who(e))} (ID {e.user_id})\nПричина: {esc(e.reason or '—')}\n"
            f"Добавлен: {e.created_at:%d.%m.%Y}" + (f"\nГолосов «спам»: {votes}" if votes else ""))
    await reply(target, text, kb([[("♻️ Убрать из списка", f"bl:rm:{e.user_id}")],
                                  [("◀️ К списку", f"bl:ls:{e.status}:0")]]))


@router.callback_query(F.data.regexp(r"^bl:o:-?\d{1,19}$"))
async def open_entry(cb: CallbackQuery) -> None:
    await card(cb, int(cb.data.split(":")[2]))


@router.callback_query(F.data.regexp(r"^bl:rm:-?\d{1,19}$"))
async def remove(cb: CallbackQuery) -> None:
    """Убрать из списка. Голоса «спам» тоже снимаем — иначе первый же новый голос вернул бы автора в чёрный список."""
    uid = int(cb.data.split(":")[2])
    async with Session() as s:
        e = await s.get(BlacklistEntry, uid)
        status = e.status if e else "spam"
        if e:
            await s.delete(e)
        await s.execute(delete(SpamVote).where(SpamVote.author_id == uid))
        await s.commit()
    await cb.answer("Убрано из списка")
    cb.data = f"bl:ls:{status}:0"
    await show_list(cb)


@router.callback_query(F.data == "bl:add")
async def add(cb: CallbackQuery, state: FSMContext) -> None:
    await ask(cb, state, "bl_add", "Telegram ID пользователя (числом):")


@router.callback_query(F.data == "bl:find")
async def find(cb: CallbackQuery, state: FSMContext) -> None:
    await ask(cb, state, "bl_find", "ID или @ник автора:")


async def bl_find(s, m: Message, d, text):
    raw = text.strip().lstrip("@")
    if not raw:
        return await m.answer("Напишите ID или @ник") and False
    if raw.lstrip("-").isascii() and raw.lstrip("-").isdigit() and len(raw) < 20:
        e = await s.get(BlacklistEntry, int(raw))
    else:
        e = await s.scalar(select(BlacklistEntry).where(func.lower(BlacklistEntry.username) == raw.lower()).limit(1))
    if e is None:
        return await m.answer("Не нашёл в списках.", reply_markup=kb([[("◀️ Назад", "bl:menu")]]))
    await card(m, e.user_id)
