"""📣 Рассылка: администратор пишет всем, только платным или только бесплатным пользователям."""
import asyncio
import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery
from sqlalchemy import select

from app.bot.common import ask, esc, kb, reply
from app.db import Session
from app.models import User
from app.services.distribute import is_paid

log = logging.getLogger(__name__)
router = Router()
AUDIENCES = {"all": "всем", "paid": "платным 💎", "free": "бесплатным 🆓"}
_pending: dict[int, tuple[str, str]] = {}  # id администратора -> (аудитория, текст)
_busy = False


async def recipients(aud: str) -> list[int]:
    async with Session() as s:
        users = (await s.scalars(select(User).where(User.onboarded, ~User.blocked))).all()
    return [u.tg_id for u in users if aud == "all" or (aud == "paid") == is_paid(u)]


async def broadcast_menu(target: CallbackQuery) -> None:
    await reply(target, "📣 <b>Рассылка</b>\nКому отправить сообщение?", kb(
        [[(f"👥 Всем", "bc:aud:all")], [("💎 Только платным", "bc:aud:paid"), ("🆓 Только бесплатным", "bc:aud:free")],
         [("◀️ Назад", "bc:back")]]))


@router.callback_query(F.data == "bc:back")
async def back(cb: CallbackQuery) -> None:
    from app.bot.setup import admin_panel

    await reply(cb, "🛠 <b>Управление ботом</b>", admin_panel())


@router.callback_query(F.data.regexp(r"^bc:aud:(all|paid|free)$"))
async def choose(cb: CallbackQuery, state: FSMContext) -> None:
    aud = cb.data.split(":")[2]
    count = len(await recipients(aud))
    await ask(cb, state, "bc_text", f"Текст сообщения для рассылки ({AUDIENCES[aud]}, получателей: {count}). "
              "Можно использовать форматирование Telegram:", aud=aud)


async def bc_text(s, m, d, text):
    """Ввод текста (из inputs.py): показываем предпросмотр и просим подтвердить."""
    aud = d["aud"]
    _pending[m.from_user.id] = (aud, text)
    count = len(await recipients(aud))
    await m.answer(f"📣 <b>Предпросмотр</b> ({AUDIENCES[aud]}, получателей: {count}):\n\n{text}",
                   reply_markup=kb([[(f"✅ Отправить ({count})", "bc:go"), ("❌ Отмена", "bc:no")]]))


@router.callback_query(F.data == "bc:no")
async def cancel(cb: CallbackQuery) -> None:
    _pending.pop(cb.from_user.id, None)
    await cb.message.edit_reply_markup(reply_markup=kb([[("✔ Отменено", "noop")]]))
    await cb.answer()


@router.callback_query(F.data == "bc:go")
async def go(cb: CallbackQuery) -> None:
    global _busy
    item = _pending.pop(cb.from_user.id, None)
    if item is None:
        return await cb.answer("Нет сообщения для отправки", show_alert=True)
    if _busy:
        _pending[cb.from_user.id] = item
        return await cb.answer("Предыдущая рассылка ещё идёт", show_alert=True)
    aud, text = item
    await cb.message.edit_reply_markup(reply_markup=kb([[("✔ Отправляю…", "noop")]]))
    await cb.answer()
    asyncio.create_task(_send_all(cb.bot, cb.from_user.id, aud, text))


async def _send_all(bot, admin_id: int, aud: str, text: str) -> tuple[int, int]:
    global _busy
    _busy = True
    ok = fail = 0
    try:
        for uid in await recipients(aud):
            try:
                try:
                    await bot.send_message(uid, text, disable_web_page_preview=True)
                except TelegramRetryAfter as e:  # Telegram просит подождать — ждём и пробуем ещё раз
                    await asyncio.sleep(e.retry_after + 1)
                    await bot.send_message(uid, text, disable_web_page_preview=True)
                ok += 1
            except TelegramForbiddenError:
                fail += 1
                async with Session() as s:
                    u = await s.get(User, uid)
                    if u:
                        u.blocked = True
                        await s.commit()
            except Exception:
                fail += 1
                log.info("рассылка: не доставлено %s", uid)
            await asyncio.sleep(0.06)  # ~16 сообщений в секунду, в пределах лимитов Telegram
    finally:
        _busy = False
    await bot.send_message(admin_id, f"📣 Рассылка завершена: доставлено {ok}, не доставлено {fail}.")
    return ok, fail
