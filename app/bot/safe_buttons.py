"""Кнопка-ссылка на человека без @ника (tg://user?id=…) иногда не принимается Telegram: BUTTON_USER_INVALID
(человек скрыл аккаунт настройками приватности, бот его «не знает»). Тогда падало ВСЁ сообщение — например, заявка
не доходила. Здесь ловим эту ошибку для любого запроса бота и повторяем без такой кнопки, дописав ID в текст."""
import logging

from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardMarkup

log = logging.getLogger(__name__)
PREFIX = "tg://user?id="


def strip_user_buttons(markup: InlineKeyboardMarkup) -> tuple[InlineKeyboardMarkup, list[str]]:
    """Клавиатура без кнопок tg://user и список ID из них."""
    ids, rows = [], []
    for row in markup.inline_keyboard:
        keep = []
        for b in row:
            if (b.url or "").startswith(PREFIX):
                ids.append(b.url[len(PREFIX):])
            else:
                keep.append(b)
        if keep:
            rows.append(keep)
    return InlineKeyboardMarkup(inline_keyboard=rows), ids


class UserButtonFix(BaseRequestMiddleware):
    async def __call__(self, make_request, bot, method):
        try:
            return await make_request(bot, method)
        except TelegramBadRequest as e:
            markup = getattr(method, "reply_markup", None)
            if "BUTTON_USER_INVALID" not in str(e) or not isinstance(markup, InlineKeyboardMarkup):
                raise
            fixed, ids = strip_user_buttons(markup)
            if not ids:
                raise
            note = "\n\n✍️ У человека нет @ника и он скрыл профиль — ответьте в группе или найдите по ID: " + ", ".join(ids)
            update = {"reply_markup": fixed if fixed.inline_keyboard else None}
            for field in ("text", "caption"):
                value = getattr(method, field, None)
                if isinstance(value, str):
                    update[field] = value + note
            log.warning("кнопка-ссылка на человека не принята Telegram — отправляю без неё")
            return await make_request(bot, method.model_copy(update=update))
