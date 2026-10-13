import html

from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo

esc = html.escape

# Кнопки меню: у пользователя — свои разделы, у администратора — ещё и управление ботом
USER_MENU = [
    "📥 Заявки", "👥 Клиенты", "🎯 Мои ниши", "📍 Мой город", "🌐 Каталог", "👤 Профиль", "📊 Статистика", "❓ Помощь",
]
# админские разделы спрятаны в «🛠 Управление», чтобы клавиатура была компактной
ADMIN_SECTIONS = ["🔍 Группы", "🗂 Категории", "🔑 Ключевые слова", "🛡 Чёрный список", "👥 Пользователи", "⚙️ Настройки"]
ADMIN_MENU = USER_MENU + ["🛠 Управление"]
ADMIN_ONLY = set(ADMIN_SECTIONS) | {"🛠 Управление"}
MENU = set(ADMIN_MENU) | set(ADMIN_SECTIONS)


class Ask(StatesGroup):
    value = State()  # ждём текстовый ввод; что с ним делать — в data["action"]


class AccountFlow(StatesGroup):
    name = State()
    api_id = State()
    api_hash = State()
    phone = State()
    proxy = State()
    code = State()
    password = State()


def kb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    """(текст, callback_data), (текст, 'url:https://...') либо (текст, 'webapp:https://...') — мини-приложение."""
    def btn(t: str, d: str) -> InlineKeyboardButton:
        if d.startswith("url:"):
            return InlineKeyboardButton(text=t, url=d[4:])
        if d.startswith("webapp:"):
            return InlineKeyboardButton(text=t, web_app=WebAppInfo(url=d[7:]))
        return InlineKeyboardButton(text=t, callback_data=d)

    return InlineKeyboardMarkup(inline_keyboard=[[btn(t, d) for t, d in row] for row in rows])


async def reply(target: Message | CallbackQuery, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    """Для сообщений — новое сообщение, для кнопок — правим текущее."""
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=markup, disable_web_page_preview=True)
        except Exception:  # «message is not modified» и т.п.
            await target.message.answer(text, reply_markup=markup, disable_web_page_preview=True)
        await target.answer()
    else:
        await target.answer(text, reply_markup=markup, disable_web_page_preview=True)


async def ask(target: Message | CallbackQuery, state, action: str, prompt: str, **data) -> None:
    await state.set_state(Ask.value)
    await state.update_data(action=action, **data)
    msg = target.message if isinstance(target, CallbackQuery) else target
    await msg.answer(f"{prompt}\n\n/cancel — отмена")
    if isinstance(target, CallbackQuery):
        await target.answer()


def split_words(raw: str) -> list[str]:
    return [w.strip() for w in raw.replace("\n", ",").split(",") if w.strip()]
