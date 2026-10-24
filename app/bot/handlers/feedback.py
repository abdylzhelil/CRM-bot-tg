"""💬 Сообщения пользователей владельцу и ответы на них. Логика — services/feedback.py.

Человеку: свободный текст в боте (например, ответ на рассылку) или кнопка «💬 Написать владельцу» → сообщение
сохраняется и приходит админу. Админу: 🛠 Управление → «💬 Сообщения», кнопки «↩️ Ответить» и «✔ Прочитано».
"""
import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app import config
from app.bot import notify
from app.bot.common import ask, esc, kb, reply
from app.db import Session
from app.models import Feedback, User
from app.services import feedback

router = Router()        # всем
admin_router = Router()  # админу
log = logging.getLogger(__name__)


def _who(u: User | None, uid: int) -> str:
    if u is None:
        return f"ID {uid}"
    return esc(u.name or "без имени") + (f" (@{esc(u.username)})" if u.username else "") + f", ID {uid}"


def _card(fb: Feedback, u: User | None) -> tuple[str, object]:
    text = (f"💬 <b>Сообщение от {_who(u, fb.user_id)}</b>\n{esc(fb.text)}"
            + (f"\n\n↩️ Вы ответили: {esc(fb.answer)}" if fb.answer else ""))
    rows = [[("↩️ Ответить", f"fb:reply:{fb.id}")] + ([] if fb.read else [("✔ Прочитано", f"fb:read:{fb.id}")])]
    return text, kb(rows)


async def receive(m: Message, user: User, text: str) -> bool:
    """Сохранить сообщение человека и показать админу. False — лимит на сутки исчерпан."""
    async with Session() as s:
        fb = await feedback.save(s, user, text)
        if fb is None:
            return False
        await s.commit()
        card, markup = _card(fb, user)
    for uid in config.OWNER_IDS:
        try:
            await notify._bot.send_message(uid, card, reply_markup=markup)
        except Exception:
            log.info("сообщение пользователя не показано админу %s", uid)
    return True


async def on_free_text(m: Message, user: User) -> bool:
    """Из «непонятного сообщения» (fallback): обычный текст человека — это сообщение владельцу. True — обработано."""
    text = (m.text or "").strip()
    if user.is_admin or len(text) < 2 or text.startswith("/"):
        return False
    if not await receive(m, user, text):
        return False
    from app.bot.setup import menu_keyboard

    await m.answer("✉️ Спасибо! Передал владельцу бота — ответ придёт сюда же.\nМеню — ниже 👇", reply_markup=menu_keyboard(user))
    return True


@router.callback_query(F.data == "fb:write")
async def fb_write(cb: CallbackQuery, state: FSMContext) -> None:
    await ask(cb, state, "feedback", "💬 Напишите сообщение владельцу бота: вопрос, идею, что неудобно. Ответ придёт сюда же.")


async def on_input(s, m: Message, d, text: str):
    """Ввод после «💬 Написать владельцу» (inputs.HANDLERS["feedback"])."""
    user = await s.get(User, m.from_user.id)
    if not await receive(m, user, text):
        return await m.answer("Сегодня вы уже написали много сообщений — продолжим завтра 🙏")
    await m.answer("✉️ Отправлено владельцу бота. Ответ придёт сюда же.")


# ---- админ ----
async def show_list(target: Message | CallbackQuery, unread_only: bool = True) -> None:
    async with Session() as s:
        rows = await feedback.recent(s, unread_only)
        unread = await feedback.unread_count(s)
    head = (f"💬 <b>Сообщения от пользователей</b> — новых: {unread}\n"
            "Сюда попадают ответы на рассылку и всё, что люди пишут боту текстом.")
    if not rows:
        return await reply(target, head + ("\n\nНовых нет." if unread_only else "\n\nПока никто не писал."),
                           kb([[("📜 Все сообщения", "fb:all")]] if unread_only else []))
    await reply(target, head + "\n\nПоследние ниже 👇",
                kb([[("📜 Все (последние 10)" if unread_only else "🆕 Только новые", "fb:all" if unread_only else "fb:list")]]))
    msg = target.message if isinstance(target, CallbackQuery) else target
    for fb, u in rows:
        text, markup = _card(fb, u)
        await msg.answer(text, reply_markup=markup)


@admin_router.callback_query(F.data.in_({"fb:list", "fb:all"}))
async def fb_list(cb: CallbackQuery) -> None:
    await show_list(cb, unread_only=cb.data == "fb:list")


@admin_router.callback_query(F.data.regexp(r"^fb:read:\d+$"))
async def fb_read(cb: CallbackQuery) -> None:
    async with Session() as s:
        fb = await s.get(Feedback, int(cb.data.split(":")[2]))
        if fb is not None:
            fb.read = True
            await s.commit()
    await cb.answer("Отмечено прочитанным")
    await cb.message.edit_reply_markup(reply_markup=kb([[("↩️ Ответить", f"fb:reply:{cb.data.split(':')[2]}")]]))


@admin_router.callback_query(F.data.regexp(r"^fb:reply:\d+$"))
async def fb_reply(cb: CallbackQuery, state: FSMContext) -> None:
    await ask(cb, state, "fb_reply", "↩️ Напишите ответ — бот перешлёт его человеку от имени КлиентРадара:",
              fid=int(cb.data.split(":")[2]))


async def on_reply(s, m: Message, d, text: str):
    """Ответ админа (inputs.HANDLERS["fb_reply"])."""
    fb = await feedback.answer(s, d["fid"], text)
    if fb is None:
        return await m.answer("Сообщение не найдено (удалено?).")
    await s.commit()
    try:
        await notify._bot.send_message(fb.user_id, f"💬 <b>Ответ от КлиентРадара:</b>\n{esc(text)}",
                                       reply_markup=kb([[("✍️ Ответить", "fb:write")]]))
    except Exception:
        return await m.answer("⚠️ Не доставлено: человек заблокировал бота или удалил аккаунт. Ответ сохранён.")
    await m.answer("✔ Ответ отправлен.", reply_markup=kb([[("💬 Сообщения", "fb:list")]]))
