from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
import logging

from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand, BotCommandScopeChat, BotCommandScopeDefault, KeyboardButton, MenuButtonCommands, Message,
    ReplyKeyboardMarkup,
)

from app import config
from app.bot import notify
from app.bot.access import IsAdmin, UserMiddleware
from app.bot.common import ADMIN_MENU, ADMIN_ONLY, MENU, USER_MENU
from app.bot.handlers import (
    accounts, blacklist, categories, clients, groups, ingroup, inputs, keywords, leads, profile, settings, stats,
    users_admin,
)
from app.models import User


log = logging.getLogger(__name__)

HELP = (
    "❓ <b>Как пользоваться</b>\n\n"
    "Бот читает Telegram-группы и присылает сообщения людей, которые <b>ищут исполнителя</b> по вашим нишам "
    "и в вашем городе. Спам, рекламу конкурентов и повторы отсеивает сам.\n\n"
    "<b>Карточка заявки:</b>\n"
    "✍️ Ответить — написать человеку в личку\n"
    "📋 Шаблон — готовый текст ответа с вашими ценами (нажмите на текст — скопируется)\n"
    "✅ В работу — клиент попадает в 👥 Клиенты, там этапы сделки, заметки и напоминания\n"
    "⏰ Отложить · ❌ Отказ · 🚫 Спам — автор спама вам больше не придёт\n\n"
    "<b>Настройки</b> — в 👤 Профиль: ниши, город, минус-слова, режим тишины, свой шаблон и цены, пауза.\n"
    "Заявка живёт минуты — отвечайте быстро 🔥\n\n"
    "Команды: /menu — меню, /leads — заявки, /clients — клиенты, /profile — профиль, /cancel — отмена ввода."
)

USER_COMMANDS = [
    BotCommand(command="menu", description="Главное меню"),
    BotCommand(command="leads", description="Мои заявки"),
    BotCommand(command="clients", description="Мои клиенты (CRM)"),
    BotCommand(command="profile", description="Профиль и настройки"),
    BotCommand(command="help", description="Как пользоваться"),
    BotCommand(command="cancel", description="Отменить ввод"),
]
ADMIN_COMMANDS = USER_COMMANDS[:-2] + [
    BotCommand(command="users", description="Пользователи и тарифы"),
    BotCommand(command="groups", description="Группы и разведчики"),
    BotCommand(command="settings", description="Настройки бота"),
] + USER_COMMANDS[-2:]
DESCRIPTION = (
    "Нахожу клиентов в Telegram-группах: присылаю сообщения людей, которые ищут исполнителя по вашей нише "
    "и в вашем городе. Отсеиваю спам и конкурентов, помогаю ответить и довести до сделки (встроенная CRM). "
    "Нажмите «Запустить» — настройка займёт 30 секунд."
)
SHORT_DESCRIPTION = "Заявки клиентов из Telegram-групп по вашей нише и городу + CRM"


def menu_keyboard(user: User | None = None) -> ReplyKeyboardMarkup:
    labels = ADMIN_MENU if user is not None and user.is_admin else USER_MENU
    rows = [[KeyboardButton(text=t) for t in labels[i : i + 3]] for i in range(0, len(labels), 3)]
    # клавиатуру можно свернуть значком ⌨ в поле ввода и открыть снова
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, input_field_placeholder="Выберите раздел")


def admin_panel():
    from app.bot.common import kb

    return kb([
        [("🔍 Группы", "adm:groups"), ("👥 Пользователи", "adm:users")],
        [("🗂 Категории", "adm:categories"), ("🔑 Ключевые слова", "adm:keywords")],
        [("🛡 Чёрный список", "adm:blacklist"), ("⚙️ Настройки", "adm:settings")],
    ])


async def setup_profile(bot: Bot) -> None:
    """Кнопка «Меню» с командами, описание бота до запуска. Ошибки не мешают работе."""
    try:
        await bot.set_my_commands(USER_COMMANDS, scope=BotCommandScopeDefault())
        for admin_id in config.OWNER_IDS:
            await bot.set_my_commands(ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=admin_id))
        await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
        await bot.set_my_description(DESCRIPTION)
        await bot.set_my_short_description(SHORT_DESCRIPTION)
    except Exception:
        log.warning("не удалось обновить команды/описание бота", exc_info=True)


base = Router()


@base.message(CommandStart())
@base.message(Command("menu"))
async def start(m: Message, state: FSMContext, user: User) -> None:
    await state.clear()
    if not user.onboarded:
        return await profile.show_niches(m, user)
    await m.answer("👋 Выберите раздел:", reply_markup=menu_keyboard(user))


@base.message(Command("help"))
async def help_cmd(m: Message, state: FSMContext, user: User) -> None:
    await state.clear()
    await m.answer(HELP, reply_markup=menu_keyboard(user) if user.onboarded else None)


SHORTCUTS = {"leads": "📥 Заявки", "clients": "👥 Клиенты", "profile": "👤 Профиль",
             "users": "👥 Пользователи", "groups": "🔍 Группы", "settings": "⚙️ Настройки"}


@base.message(Command(*SHORTCUTS))
async def shortcut(m: Message, state: FSMContext, user: User) -> None:
    await open_section(m, state, user, SHORTCUTS[m.text.split()[0].lstrip("/").split("@")[0]])


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
    await open_section(m, state, user, m.text)


async def open_section(m: Message, state: FSMContext, user: User, section: str) -> None:
    await state.clear()
    if not user.onboarded:
        return await profile.show_niches(m, user)
    if section in ADMIN_ONLY and not user.is_admin:
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
        "❓ Помощь": lambda: m.answer(HELP),
        "🛠 Управление": lambda: m.answer("🛠 <b>Управление ботом</b>", reply_markup=admin_panel()),
    }
    await handlers[section]()


admin_nav = Router(name="admin_nav")
ADMIN_TARGETS = {
    "groups": groups.groups_menu, "users": users_admin.users_menu, "categories": categories.categories_menu,
    "keywords": keywords.keywords_menu, "blacklist": blacklist.blacklist_menu, "settings": settings.settings_menu,
}


@admin_nav.callback_query(F.data.startswith("adm:"))
async def admin_section(cb, state: FSMContext) -> None:
    await state.clear()
    target = ADMIN_TARGETS.get(cb.data.split(":")[1])
    if target:
        await target(cb)


fallback = Router(name="fallback")


@fallback.message(StateFilter(None))
async def unknown(m: Message, user: User) -> None:
    """Любое непонятное сообщение — показать меню (чтобы не набирать /start)."""
    if not user.onboarded:
        return await profile.show_niches(m, user)
    await m.answer("Выберите раздел в меню ниже 👇 или /help — как пользоваться.", reply_markup=menu_keyboard(user))


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
        settings.router, users_admin.router, admin_nav,
    )
    # личка: всем пользователям
    private = Router(name="private")
    private.message.filter(F.chat.type == "private")
    private.include_routers(
        base, inputs.router, profile.router, leads.router, clients.router, stats.router, admin, fallback,
    )
    dp.include_routers(ingroup.router, private)
    return bot, dp
