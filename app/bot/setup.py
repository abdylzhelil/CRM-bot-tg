from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup

from app import config
from app.bot import notify
from app.bot.access import IsAdmin, UserMiddleware
from app.bot.common import ADMIN_MENU, ADMIN_ONLY, MENU, USER_MENU
from app.bot.handlers import (
    accounts, blacklist, categories, clients, groups, ingroup, inputs, keywords, leads, profile, settings, stats,
    users_admin,
)
from app.models import User


def menu_keyboard(user: User | None = None) -> ReplyKeyboardMarkup:
    labels = ADMIN_MENU if user is not None and user.is_admin else USER_MENU
    rows = [[KeyboardButton(text=t) for t in labels[i : i + 2]] for i in range(0, len(labels), 2)]
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


base = Router()


@base.message(CommandStart())
async def start(m: Message, state: FSMContext, user: User) -> None:
    await state.clear()
    if not user.onboarded:
        return await profile.show_niches(m, user)
    await m.answer("👋 Выберите раздел:", reply_markup=menu_keyboard(user))


@base.message(Command("cancel"))
async def cancel(m: Message, state: FSMContext, user: User) -> None:
    from app.bot.handlers.accounts import _pending

    pending = _pending.pop(m.from_user.id, None)
    if pending:
        await pending[0].disconnect()
    await state.clear()
    await m.answer("Отменено.", reply_markup=menu_keyboard(user))


@base.message(F.text.in_(MENU))
async def menu_in_state(m: Message, state: FSMContext, user: User) -> None:
    """Нажатие кнопки меню прерывает ввод и передаёт управление разделу."""
    await state.clear()
    if not user.onboarded:
        return await profile.show_niches(m, user)
    if m.text in ADMIN_ONLY and not user.is_admin:
        return
    handlers = {
        "📥 Заявки": lambda: leads.leads_menu(m, user),
        "👥 Клиенты": lambda: clients.clients_menu(m),
        "🎯 Мои ниши": lambda: profile.niches_menu(m, user),
        "📍 Мой город": lambda: profile.city_menu(m, user),
        "👤 Профиль": lambda: profile.profile_menu(m, user),
        "📊 Статистика": lambda: stats.stats(m, user),
        "🔍 Группы": lambda: groups.groups_menu(m),
        "🗂 Категории": lambda: categories.categories_menu(m),
        "🔑 Ключевые слова": lambda: keywords.keywords_menu(m),
        "🛡 Чёрный список": lambda: blacklist.blacklist_menu(m),
        "👥 Пользователи": lambda: users_admin.users_menu(m),
        "⚙️ Настройки": lambda: settings.settings_menu(m),
    }
    await handlers[m.text]()


def build(scout_manager) -> tuple[Bot, Dispatcher]:
    groups.manager = scout_manager
    accounts.manager = scout_manager
    bot = Bot(config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    notify.init(bot)
    dp = Dispatcher(storage=MemoryStorage())
    dp.message.outer_middleware(UserMiddleware())
    dp.callback_query.outer_middleware(UserMiddleware())

    # управление ботом — только администраторам
    admin = Router(name="admin")
    admin.message.filter(IsAdmin())
    admin.callback_query.filter(IsAdmin())
    admin.include_routers(
        accounts.router, groups.router, categories.router, keywords.router, blacklist.router,
        settings.router, users_admin.router,
    )
    # личка: всем пользователям
    private = Router(name="private")
    private.message.filter(F.chat.type == "private")
    private.include_routers(base, inputs.router, profile.router, leads.router, clients.router, stats.router, admin)
    dp.include_routers(ingroup.router, private)
    return bot, dp
