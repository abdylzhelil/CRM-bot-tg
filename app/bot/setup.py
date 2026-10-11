from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup

from app import config
from app.bot import notify
from app.bot.common import MENU
from app.bot.handlers import (
    accounts, blacklist, categories, clients, groups, ingroup, inputs, keywords, leads, settings, stats,
)


class OwnerOnly(BaseMiddleware):
    """Доступ только владельцам (по Telegram ID); остальным бот не отвечает."""

    async def __call__(self, handler, event, data):
        chat = data.get("event_chat")
        if chat and chat.type != "private":
            # сообщения групп обрабатывает только ingroup.router; меню там недоступно
            return await handler(event, data)
        user = data.get("event_from_user")
        if user and user.id in config.OWNER_IDS:
            return await handler(event, data)


def menu_keyboard() -> ReplyKeyboardMarkup:
    labels = list(MENU)
    rows = [[KeyboardButton(text=t) for t in labels[i : i + 2]] for i in range(0, len(labels), 2)]
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


base = Router()


@base.message(CommandStart())
async def start(m: Message, state: FSMContext) -> None:
    await state.clear()
    await m.answer("👋 Менеджер продаж. Выберите раздел:", reply_markup=menu_keyboard())


@base.message(Command("cancel"))
async def cancel(m: Message, state: FSMContext) -> None:
    from app.bot.handlers.accounts import _pending

    pending = _pending.pop(m.from_user.id, None)
    if pending:
        await pending[0].disconnect()
    await state.clear()
    await m.answer("Отменено.", reply_markup=menu_keyboard())


@base.message(F.text.in_(set(MENU)))
async def menu_in_state(m: Message, state: FSMContext) -> None:
    """Нажатие кнопки меню прерывает ввод и передаёт управление разделу."""
    await state.clear()
    handler = {
        "📥 Заявки": leads.leads_menu, "👥 Клиенты": clients.clients_menu, "🔍 Группы": groups.groups_menu,
        "🗂 Категории": categories.categories_menu, "🔑 Ключевые слова": keywords.keywords_menu,
        "🛡 Чёрный список": blacklist.blacklist_menu, "📊 Статистика": stats.stats,
        "⚙️ Настройки": settings.settings_menu,
    }[m.text]
    await handler(m)


def build(scout_manager) -> tuple[Bot, Dispatcher]:
    groups.manager = scout_manager
    accounts.manager = scout_manager
    bot = Bot(config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    notify.init(bot)
    dp = Dispatcher(storage=MemoryStorage())
    dp.message.outer_middleware(OwnerOnly())
    dp.callback_query.outer_middleware(OwnerOnly())
    owner = Router()
    owner.message.filter(F.chat.type == "private")
    owner.include_routers(
        base, accounts.router, inputs.router, leads.router, clients.router, groups.router,
        categories.router, keywords.router, blacklist.router, stats.router, settings.router,
    )
    dp.include_routers(ingroup.router, owner)
    return bot, dp
