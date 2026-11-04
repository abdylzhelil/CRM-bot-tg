"""BUTTON_USER_INVALID: сообщение уходит повторно без кнопки tg://user, ID дописывается в текст."""
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageText, SendMessage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.bot.safe_buttons import UserButtonFix


def markup():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✍️ Ответить", url="tg://user?id=777"),
         InlineKeyboardButton(text="📋 Шаблон", callback_data="lead:tpl:1")],
        [InlineKeyboardButton(text="✅ В работу", callback_data="lead:take:1")]])


async def test_retries_without_user_button():
    calls = []

    async def make_request(bot, method):
        calls.append(method)
        if len(calls) == 1:
            raise TelegramBadRequest(method=method, message="Bad Request: BUTTON_USER_INVALID")
        return "ok"

    res = await UserButtonFix()(make_request, None, SendMessage(chat_id=1, text="Заявка", reply_markup=markup()))
    assert res == "ok" and len(calls) == 2
    fixed = calls[1]
    urls = [b.url for row in fixed.reply_markup.inline_keyboard for b in row]
    assert not any(u and u.startswith("tg://user") for u in urls)
    assert len(fixed.reply_markup.inline_keyboard) == 2 and "777" in fixed.text


async def test_other_errors_are_not_swallowed():
    import pytest

    async def make_request(bot, method):
        raise TelegramBadRequest(method=method, message="Bad Request: message is not modified")

    with pytest.raises(TelegramBadRequest):
        await UserButtonFix()(make_request, None, EditMessageText(chat_id=1, message_id=2, text="x", reply_markup=markup()))
