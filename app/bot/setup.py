from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
import logging

from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand, BotCommandScopeChat, BotCommandScopeDefault, CallbackQuery, KeyboardButton, MenuButtonCommands, Message,
    ReplyKeyboardMarkup,
)

from app import config
from app.bot import captcha, intro, notify
from app.bot.access import IsAdmin, UserMiddleware
from app.bot.common import ADMIN_MENU, ADMIN_ONLY, MENU, USER_MENU, kb
from app.bot.handlers import (
    accounts, adposts, blacklist, board, broadcast, credits, catalog, categories, clients, licenses, payments, groups, ingroup, inputs, keywords, feedback, leads, mute, mysearch, profile, settings, stats,
    submissions, users_admin,
)
from app.db import Session
from app.models import User, now
from app.services import referrals


log = logging.getLogger(__name__)

HELP = (
    "❓ <b>Как пользоваться — 3 шага</b>\n\n"
    "1️⃣ <b>Выберите, что вы делаете</b> — «🎯 Мои ниши» (например, ремонт, юрист, репетитор) и «📍 Мой город».\n"
    "2️⃣ <b>Ждите заявки</b> — бот сам находит в Telegram людей, которые ищут мастера, и присылает их сюда.\n"
    "3️⃣ <b>Отвечайте быстро</b> — под заявкой кнопка «✍️ Ответить» (написать человеку) или «💬 Ответить в группе». "
    "Кто откликнулся первым — того и выбирают 🔥\n\n"
    "Не ваша заявка — «🙈 Не моё». Это не заказчик, а мастер рекламирует себя — «🙅 Исполнитель, не клиент» "
    "(бот научится такое не присылать). Спам — «🚫 Спам». Взяли клиента — «✅ В работу», он попадёт в «👥 Клиенты».\n\n"
    "🏆 <b>Рейтинг</b> — сколько заявок по каким нишам в вашем городе.\n"
    "➕ <b>Есть своя группа или чат?</b> Поставьте туда бота кнопкой ниже — в закрепе будет рейтинг заявок, "
    "а вам — кредиты 💰 с оплат участников.\n"
    "🤝 <b>Пригласите друга</b> — 👤 Профиль → 🤝 Пригласить: по +7 дней 💎 вам и ему.\n\n"
    "⚠️ Если в заявке просят предоплату, код из СМС или зовут на сторонний сайт — бот предупредит; обман — «🚫 Спам / схема».\n"
    "Что-то непонятно или не работает — «🆘 Сообщить о проблеме» ниже или просто напишите сюда, владелец бота ответит."
)
HELP_MORE = (
    "Команды: /menu — меню, /leads — заявки, /clients — клиенты, /profile — профиль, /invite — пригласить, "
    "/catalog — каталог, /terms — условия оплаты, /privacy — политика, /deleteme — удалить мои данные, /cancel — отмена."
)

USER_COMMANDS = [
    BotCommand(command="menu", description="Главное меню"),
    BotCommand(command="leads", description="Мои заявки"),
    BotCommand(command="clients", description="Мои клиенты (CRM)"),
    BotCommand(command="invite", description="Пригласить друзей (+дни 💎)"),
    BotCommand(command="catalog", description="Каталог групп и бонусы"),
    BotCommand(command="ads", description="📣 Заказать рекламу"),
    BotCommand(command="credits", description="💰 Мои кредиты"),
    BotCommand(command="profile", description="Профиль и настройки"),
    BotCommand(command="terms", description="Условия оплаты"),
    BotCommand(command="privacy", description="Политика конфиденциальности"),
    BotCommand(command="deleteme", description="Удалить мои данные"),
    BotCommand(command="paysupport", description="Вопросы по оплате"),
    BotCommand(command="help", description="Как пользоваться"),
    BotCommand(command="about", description="О боте"),
    BotCommand(command="cancel", description="Отменить ввод"),
]
ADMIN_COMMANDS = USER_COMMANDS[:-1] + [
    BotCommand(command="users", description="Пользователи и тарифы"),
    BotCommand(command="groups", description="Группы и разведчики"),
    BotCommand(command="settings", description="Настройки бота"),
    BotCommand(command="forget", description="Удалить данные автора заявки"),
    BotCommand(command="aitest", description="Сравнить ИИ-модели (платно)"),
] + USER_COMMANDS[-1:]
BOT_NAME = "КлиентРадар"
DESCRIPTION = (
    "📡 КлиентРадар находит клиентов в Telegram-группах.\n\n"
    "• присылает сообщения людей, которые ищут исполнителя — по вашим нишам и городу\n"
    "• отсеивает спам, рекламу и конкурентов\n"
    "• готовит ответ и ведёт сделку во встроенной CRM\n\n"
    "🆓 Есть бесплатный тариф. Нажмите «Запустить» — настройка займёт 30 секунд."
)
SHORT_DESCRIPTION = "📡 Заявки клиентов из Telegram-групп по вашей нише и городу + CRM"
ABOUT = (
    "📡 <b>КлиентРадар</b>\n\n"
    "Бот читает открытые Telegram-группы и присылает сообщения людей, которые <b>ищут исполнителя</b> "
    "по вашим нишам и в вашем городе. Спам и повторы отсеиваются сами.\n\n"
    "Дальше — ✍️ ответ в один клик, 👥 CRM с этапами сделки и напоминаниями, 🌐 каталог групп с бонусами "
    "и 🤝 приглашения друзей.\n\n"
    "Бесплатно: до 5 заявок в день с небольшой задержкой. 💎 Полный тариф — без лимита и сразу.\n\n"
    "Как пользоваться — /help, условия — /terms, политика — /privacy."
)
assert len(DESCRIPTION) <= 512 and len(SHORT_DESCRIPTION) <= 120 and len(BOT_NAME) <= 64


def menu_keyboard(user: User | None = None) -> ReplyKeyboardMarkup:
    labels = ADMIN_MENU if user is not None and user.is_admin else USER_MENU
    rows = [[KeyboardButton(text=t) for t in labels[i : i + 3]] for i in range(0, len(labels), 3)]
    # клавиатуру можно свернуть значком ⌨ в поле ввода и открыть снова
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, input_field_placeholder="Выберите раздел")


async def help_keyboard():
    from app.bot.handlers import board

    rows = [[("🏆 Рейтинг заявок", "rt:menu")]]
    link = await board.add_link()
    if link:
        rows.append([("➕ Поставить бота в мою группу", f"url:{link}")])
    rows.append([("💬 Написать владельцу", "fb:write"), ("🆘 Сообщить о проблеме", "fb:bug")])
    return kb(rows)


async def _help(m: Message):
    await m.answer(HELP, reply_markup=await help_keyboard())


def admin_panel(unread: int = 0):
    from app.bot.common import kb

    return kb([
        [(f"💬 Сообщения от пользователей{f' · новых: {unread}' if unread else ''}", "fb:list")],
        [("🔍 Группы", "adm:groups"), ("👥 Пользователи", "adm:users")],
        [("🗂 Категории", "adm:categories"), ("🔑 Ключевые слова", "adm:keywords")],
        [("🛡 Чёрный список", "adm:blacklist"), ("⚙️ Настройки", "adm:settings")],
        [("📣 Рассылка", "adm:broadcast"), ("🪪 Лицензии", "adm:licenses")],
        [("💰 Кредиты", "cra:menu")],
    ])


async def setup_profile(bot: Bot) -> None:
    """Кнопка «Меню» с командами, описание бота до запуска. Ошибки не мешают работе."""
    try:
        cabinet = [BotCommand(command="app", description="📱 Кабинет")] if config.WEBAPP_URL else []
        await bot.set_my_commands(cabinet + USER_COMMANDS, scope=BotCommandScopeDefault())
        for admin_id in config.OWNER_IDS:
            await bot.set_my_commands(cabinet + ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=admin_id))
        await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
        await bot.set_my_description(DESCRIPTION)
        await bot.set_my_short_description(SHORT_DESCRIPTION)
    except Exception:
        log.warning("не удалось обновить команды/описание бота", exc_info=True)
    try:  # имя Telegram разрешает менять редко — трогаем, только если оно отличается
        if (await bot.get_my_name()).name != BOT_NAME:
            await bot.set_my_name(BOT_NAME)
    except Exception:
        log.warning("не удалось обновить имя бота", exc_info=True)


base = Router()


@base.message(CommandStart())
@base.message(Command("menu"))
async def start(m: Message, state: FSMContext, user: User, command: CommandObject) -> None:
    await state.clear()
    await credits.claim_on_start(user)  # админ группы впервые открыл бота — его кредиты ждали здесь
    if command.args == "ads" and user.verified:  # ссылка для рекламодателей: сразу к заказу рекламы
        return await adposts.link_entry(m, user)
    referrer = referrals.parse_payload(command.args)
    if not user.onboarded:
        async with Session() as s:
            if referrer:
                await referrals.attach(s, user, referrer)
            await referrals.attach_source(s, user, command.args)
    if user.verified and await board.quick_join(m, user, command.args):  # нажал нишу в закрепе — всё готово
        return
    group_id = board.group_of(command.args)  # пришёл по кнопке из закрепа группы — сразу её рейтинг
    if command.args == "rating" or group_id:
        if user.verified:
            await board.rating_view(m, user, group_id=group_id)
        if user.onboarded:
            return
    if not user.onboarded:
        if user.verified and command.command == "start" and not group_id:  # непроверенным анимацию — после «я не бот»
            await intro.send_radar(m)
        return await profile.show_niches(m, user)
    await m.answer("📡 Главное меню — выберите раздел:", reply_markup=menu_keyboard(user))


@base.message(Command("radar"))
async def radar_cmd(m: Message, user: User) -> None:
    """Показать радар-анимацию (новым она приходит на /start сама)."""
    await intro.send_radar(m)


@base.message(Command("app"))
async def app_cmd(m: Message, state: FSMContext, user: User) -> None:
    await state.clear()
    if not config.WEBAPP_URL:
        return await m.answer("📱 Кабинет пока не подключён. Всё доступно в меню бота.")
    await m.answer("📱 <b>Кабинет</b> — заявки, клиенты и настройки в одном окне.",
                   reply_markup=kb([[("📱 Открыть кабинет", f"webapp:{config.WEBAPP_URL}/app/")]]))


@base.message(Command("about"))
async def about_cmd(m: Message, state: FSMContext, user: User) -> None:
    await state.clear()
    await m.answer(ABOUT, reply_markup=menu_keyboard(user) if user.onboarded else None)


@base.message(Command("help"))
async def help_cmd(m: Message, state: FSMContext, user: User) -> None:
    await state.clear()
    await m.answer(HELP + "\n\n" + HELP_MORE, reply_markup=await help_keyboard())


SHORTCUTS = {"leads": "📥 Заявки", "clients": "👥 Клиенты", "profile": "👤 Профиль", "catalog": "🌐 Каталог",
             "users": "👥 Пользователи", "groups": "🔍 Группы", "settings": "⚙️ Настройки"}


@base.message(Command(*SHORTCUTS))
async def shortcut(m: Message, state: FSMContext, user: User) -> None:
    await open_section(m, state, user, SHORTCUTS[m.text.split()[0].lstrip("/").split("@")[0]])


PRIVACY_URL = "https://telegra.ph/Politika-konfidencialnosti-KlientRadar-10-13-2"


@base.message(Command("privacy"))
async def privacy(m: Message) -> None:
    await m.answer(f'🔒 <a href="{PRIVACY_URL}">Политика конфиденциальности</a>: какие данные мы храним и как их удалить.')


@base.message(Command("deleteme"))
async def deleteme(m: Message, user: User) -> None:
    if user.is_admin:
        return await m.answer("Администратора удалить нельзя.")
    paid = user.plan == "full" and user.plan_until is not None and user.plan_until > now()
    warn = "\n⚠️ У вас оплаченный 💎 тариф — он тоже пропадёт без возврата." if paid else ""
    total = (getattr(user, "credits", 0) or 0) + (getattr(user, "credits_hold", 0) or 0)
    if total:
        warn += f"\n⚠️ На счёте {total} кредитов 💰 — они сгорят. Сначала можно обменять их: /credits"
    await m.answer(
        "🗑 <b>Удалить мои данные?</b>\nБудут удалены профиль, настройки, полученные заявки и ваши клиенты в CRM. "
        f"Вернуть их будет нельзя.{warn}\nПотом можно зарегистрироваться заново.",
        reply_markup=kb([[("🗑 Да, удалить всё", "del:yes"), ("Отмена", "del:no")]]))


@base.callback_query(F.data == "del:no")
async def delete_no(cb: CallbackQuery) -> None:
    await cb.message.edit_text("Хорошо, ничего не удалено.")
    await cb.answer()


@base.callback_query(F.data == "del:yes")
async def delete_yes(cb: CallbackQuery) -> None:
    from app.services import erase

    async with Session() as s:
        ok = await erase.erase_user(s, cb.from_user.id)
    await cb.message.edit_text("✅ Ваши данные удалены. Если захотите вернуться — отправьте /start."
                               if ok else "Этот аккаунт удалить нельзя.")
    await cb.answer()


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
        "🌐 Каталог": lambda: catalog.catalog_menu(m, user),
        "📊 Статистика": lambda: stats.stats(m, user),
        "🏆 Рейтинг": lambda: board.rating_view(m, user),
        "🔍 Группы": lambda: groups.groups_menu(m),
        "🗂 Категории": lambda: categories.categories_menu(m),
        "🔑 Ключевые слова": lambda: keywords.keywords_menu(m),
        "🛡 Чёрный список": lambda: blacklist.blacklist_menu(m),
        "👥 Пользователи": lambda: users_admin.users_menu(m),
        "⚙️ Настройки": lambda: settings.settings_menu(m),
        "❓ Помощь": lambda: _help(m),
        "🛠 Управление": lambda: _admin_home(m),
    }
    await handlers[section]()


async def _admin_home(m: Message) -> None:
    from app.services import feedback as feedback_svc

    async with Session() as s:
        unread = await feedback_svc.unread_count(s)
    await m.answer("🛠 <b>Управление ботом</b>", reply_markup=admin_panel(unread))


admin_nav = Router(name="admin_nav")
ADMIN_TARGETS = {
    "groups": groups.groups_menu, "users": users_admin.users_menu, "categories": categories.categories_menu,
    "keywords": keywords.keywords_menu, "blacklist": blacklist.blacklist_menu, "settings": settings.settings_menu,
    "broadcast": broadcast.broadcast_menu, "licenses": licenses.licenses_menu,
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
    from app.bot.handlers import feedback as feedback_h

    if await feedback_h.on_free_text(m, user):  # обычный текст — сообщение владельцу (ответ на рассылку и т. п.)
        return
    await m.answer("Выберите раздел в меню ниже 👇 или /help — как пользоваться.", reply_markup=menu_keyboard(user))


def build(scout_manager) -> tuple[Bot, Dispatcher]:
    groups.manager = scout_manager
    accounts.manager = scout_manager
    adposts.manager = scout_manager
    bot = Bot(config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    from app.bot.safe_buttons import UserButtonFix

    bot.session.middleware(UserButtonFix())  # кнопка «Ответить» человеку без ника не роняет сообщение (BUTTON_USER_INVALID)
    notify.init(bot)
    dp = Dispatcher(storage=MemoryStorage())
    dp.message.outer_middleware(UserMiddleware())
    dp.callback_query.outer_middleware(UserMiddleware())

    # управление ботом — только администраторам
    admin = Router(name="admin")
    admin.message.filter(IsAdmin())
    admin.callback_query.filter(IsAdmin())
    admin.include_routers(
        accounts.router, adposts.admin_router, groups.router, categories.router, keywords.router, blacklist.router,
        settings.router, users_admin.router, submissions.router, broadcast.router, licenses.router, mysearch.admin_router, feedback.admin_router, board.admin_router, credits.admin_router, admin_nav,
    )
    # личка: всем пользователям
    private = Router(name="private")
    private.message.filter(F.chat.type == "private")
    private.include_routers(
        captcha.router, base, inputs.router, profile.router, catalog.router, mysearch.router, mute.router, feedback.router, board.router, credits.router, adposts.router, payments.router, leads.router, clients.router, stats.router, admin, fallback,
    )
    dp.include_routers(board.group_router, ingroup.router, payments.checkout, private)

    @dp.errors()
    async def on_error(event) -> bool:
        """Любая необработанная ошибка в обработчике: в лог и тревога администратору (не чаще раза в 15 минут)."""
        log.error("ошибка обработчика", exc_info=event.exception)
        detail = str(event.exception).splitlines()[0][:160] if str(event.exception) else ""
        await notify.alert_once(f"handler:{type(event.exception).__name__}",
                                f"Ошибка в боте: {type(event.exception).__name__}" + (f" — {detail}" if detail else "")
                                + " (подробности в логах сервера).")
        return True
    return bot, dp
