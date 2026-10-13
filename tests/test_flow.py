"""Сквозной сценарий через диспетчер aiogram с фальшивым Telegram: регистрация, меню, права."""
from datetime import datetime
from itertools import count

import pytest
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, EditMessageText, GetMe, GetMyName, SendMessage, TelegramMethod
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from sqlalchemy import select

from app.bot.setup import build
from app.models import Category, UserCategory
from app.models import User as DbUser
from tests.conftest import ADMIN_ID

_ids = count(1)


class FakeSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.requests: list[TelegramMethod] = []

    async def make_request(self, bot, method, timeout=None):
        self.requests.append(method)
        if isinstance(method, GetMe):
            return User(id=42, is_bot=True, first_name="CRM", username="crm_test_bot")
        if isinstance(method, SendMessage | EditMessageText):
            return Message(message_id=next(_ids), date=datetime.now(),
                           chat=Chat(id=method.chat_id or 0, type="private"), text=method.text)
        if isinstance(method, GetMyName):
            from aiogram.types import BotName

            return BotName(name="старое имя")
        if isinstance(method, AnswerCallbackQuery):
            return True
        return True

    async def stream_content(self, *args, **kwargs):  # pragma: no cover
        yield b""

    async def close(self) -> None:
        pass

    def texts(self, chat_id: int) -> list[str]:
        return [r.text for r in self.requests
                if isinstance(r, SendMessage | EditMessageText) and r.chat_id == chat_id]


_BUILT = None


@pytest.fixture
def app_bot(maker):
    """Роутеры aiogram можно подключить к диспетчеру один раз — диспетчер общий для тестов."""
    global _BUILT
    from app.bot import notify

    if _BUILT is None:
        _BUILT = build(None)
    bot, dp = _BUILT
    session = FakeSession()
    bot.session = session
    notify.init(bot)
    return bot, dp, session


def text_update(uid: int, text: str) -> Update:
    return Update(update_id=next(_ids), message=Message(
        message_id=next(_ids), date=datetime.now(), chat=Chat(id=uid, type="private"),
        from_user=User(id=uid, is_bot=False, first_name="Тест"), text=text,
    ))


def cb_update(uid: int, data: str) -> Update:
    msg = Message(message_id=next(_ids), date=datetime.now(), chat=Chat(id=uid, type="private"), text="x")
    return Update(update_id=next(_ids), callback_query=CallbackQuery(
        id=str(next(_ids)), from_user=User(id=uid, is_bot=False, first_name="Тест"), chat_instance="c",
        message=msg, data=data,
    ))


async def pass_captcha(bot, dp, uid: int) -> None:
    """Пройти проверку «я не бот»: нажать нужный значок."""
    from app.bot import captcha

    icons, target, _ = captcha._pending[uid]
    await dp.feed_update(bot, cb_update(uid, f"cap:{icons.index(target)}"))


async def test_new_user_onboarding_flow(app_bot, maker):
    bot, dp, api = app_bot
    await dp.feed_update(bot, text_update(500, "/start"))
    assert "Проверка" in api.texts(500)[-1]  # сначала «я не бот»
    await dp.feed_update(bot, text_update(500, "привет"))  # пока проверки нет — ничего, кроме неё, не работает
    assert "Проверка" in api.texts(500)[-1]
    from app.bot import captcha

    icons, target, _ = captcha._pending[500]
    wrong = next(i for i, ic in enumerate(icons) if ic != target)
    await dp.feed_update(bot, cb_update(500, f"cap:{wrong}"))
    async with maker() as s:
        assert not (await s.get(DbUser, 500)).verified  # неверный значок — проверка не пройдена
    await pass_captcha(bot, dp, 500)
    assert "Привет" in api.texts(500)[-1] and "ниши" in api.texts(500)[-1]

    async with maker() as s:
        sites = await s.scalar(select(Category.id).where(Category.name == "Сайты"))
    await dp.feed_update(bot, cb_update(500, "ob:done"))  # без ниши нельзя
    await dp.feed_update(bot, cb_update(500, f"ob:cat:{sites}"))
    await dp.feed_update(bot, cb_update(500, "ob:done"))
    assert "Город" in api.texts(500)[-1]
    await dp.feed_update(bot, cb_update(500, "ob:city:0"))  # Махачкала
    assert any("Готово" in t for t in api.texts(500))
    async with maker() as s:
        u = await s.get(DbUser, 500)
        assert u.onboarded and u.city == "Махачкала" and not u.is_admin
        assert await s.get(UserCategory, (500, sites))


async def test_admin_sections_hidden_from_users(app_bot, maker):
    bot, dp, api = app_bot
    await dp.feed_update(bot, text_update(501, "/start"))
    await pass_captcha(bot, dp, 501)
    async with maker() as s:
        (await s.get(DbUser, 501)).onboarded = True
        await s.commit()
    before = len(api.texts(501))
    await dp.feed_update(bot, text_update(501, "🔍 Группы"))
    await dp.feed_update(bot, text_update(501, "👥 Пользователи"))
    await dp.feed_update(bot, cb_update(501, "us:plan:501:0"))  # попытка выдать себе тариф
    assert len(api.texts(501)) == before
    async with maker() as s:
        assert (await s.get(DbUser, 501)).plan == "free"

    await dp.feed_update(bot, text_update(501, "👤 Профиль"))
    assert "Профиль" in api.texts(501)[-1]


async def test_admin_can_manage_users(app_bot, maker):
    bot, dp, api = app_bot
    await dp.feed_update(bot, text_update(502, "/start"))
    await pass_captcha(bot, dp, 502)
    await dp.feed_update(bot, text_update(ADMIN_ID, "👥 Пользователи"))
    assert "Пользователей" in api.texts(ADMIN_ID)[-1]
    await dp.feed_update(bot, cb_update(ADMIN_ID, "us:plan:502:30"))
    async with maker() as s:
        u = await s.get(DbUser, 502)
        assert u.plan == "full" and u.plan_until is not None
    assert any("Полный тариф" in t for t in api.texts(502))


async def test_no_start_needed_unknown_text_shows_menu(app_bot, maker):
    bot, dp, api = app_bot
    await dp.feed_update(bot, text_update(ADMIN_ID, "привет"))
    last = [r for r in api.requests if isinstance(r, SendMessage) and r.chat_id == ADMIN_ID][-1]
    assert "меню" in last.text and not last.reply_markup.is_persistent  # панель можно свернуть
    labels = [b.text for row in last.reply_markup.keyboard for b in row]
    assert "🛠 Управление" in labels and "❓ Помощь" in labels and "👥 Пользователи" not in labels
    await dp.feed_update(bot, text_update(ADMIN_ID, "🛠 Управление"))
    await dp.feed_update(bot, cb_update(ADMIN_ID, "adm:users"))
    assert "Пользователей" in api.texts(ADMIN_ID)[-1]

    await dp.feed_update(bot, text_update(ADMIN_ID, "/leads"))
    assert "Заявки" in api.texts(ADMIN_ID)[-1]
    await dp.feed_update(bot, text_update(ADMIN_ID, "/help"))
    assert "Как пользоваться" in api.texts(ADMIN_ID)[-1]


async def test_user_menu_has_no_admin_buttons_and_profile_is_set(app_bot, maker):
    from aiogram.methods import SetMyCommands, SetMyDescription

    from app.bot.setup import setup_profile

    bot, dp, api = app_bot
    await dp.feed_update(bot, text_update(503, "/start"))
    await pass_captcha(bot, dp, 503)
    async with maker() as s:
        (await s.get(DbUser, 503)).onboarded = True
        await s.commit()
    await dp.feed_update(bot, text_update(503, "что тут"))
    last = [r for r in api.requests if isinstance(r, SendMessage) and r.chat_id == 503][-1]
    labels = [b.text for row in last.reply_markup.keyboard for b in row]
    assert "🛠 Управление" not in labels and "📥 Заявки" in labels
    await dp.feed_update(bot, cb_update(503, "adm:users"))  # чужой не откроет управление
    assert not any("Пользователей" in t for t in api.texts(503))

    await setup_profile(bot)
    assert any(isinstance(r, SetMyCommands) for r in api.requests)
    assert any(isinstance(r, SetMyDescription) for r in api.requests)


async def test_about_command_and_profile_texts(app_bot):
    from aiogram.methods import SetMyName

    from app import config
    from app.bot import setup

    bot, dp, api = app_bot
    assert len(setup.DESCRIPTION) <= 512 and len(setup.SHORT_DESCRIPTION) <= 120
    await dp.feed_update(bot, text_update(ADMIN_ID, "/about"))
    assert "КлиентРадар" in api.texts(ADMIN_ID)[-1]
    await setup.setup_profile(bot)
    assert any(isinstance(r, SetMyName) for r in api.requests)
    assert any(c.command == "about" for c in setup.USER_COMMANDS)


async def test_settings_status_presets_and_search(app_bot, maker):
    from app.services import settings as st

    for k in st.LABELS:
        assert k in st.HINTS
    for _, _, _, vals in st.PRESETS.values():
        assert all(k in st.DEFAULTS and v.isdigit() for k, v in vals.items())

    bot, dp, api = app_bot
    await dp.feed_update(bot, cb_update(ADMIN_ID, "st:status"))
    assert any("Состояние бота" in t for t in api.texts(ADMIN_ID))
    await dp.feed_update(bot, cb_update(ADMIN_ID, "st:pre:careful"))
    assert "изменит" in api.texts(ADMIN_ID)[-1]
    await dp.feed_update(bot, cb_update(ADMIN_ID, "st:preok:careful"))
    async with maker() as s:
        assert await st.get(s, "default_join_limit") == "5"
    await dp.feed_update(bot, cb_update(ADMIN_ID, "st:def:default_join_limit"))
    async with maker() as s:
        assert await st.get(s, "default_join_limit") == "10"
    await dp.feed_update(bot, cb_update(ADMIN_ID, "st:find"))
    await dp.feed_update(bot, text_update(ADMIN_ID, "возврат"))
    assert "Нашлось" in api.texts(ADMIN_ID)[-1]
    assert st.search("возврат") == ["refund_days"] or "refund_days" in st.search("возврат")


async def test_autosetup_for_user_and_admin(app_bot, maker):
    from sqlalchemy import select

    from app.models import UserNegative
    from app.services import autosetup

    bot, dp, api = app_bot
    await dp.feed_update(bot, text_update(504, "/start"))
    await pass_captcha(bot, dp, 504)
    async with maker() as s:
        u = await s.get(DbUser, 504)
        u.onboarded, u.paused = True, True
        await s.commit()
    await dp.feed_update(bot, cb_update(504, "pf:auto"))
    assert "Бесплатный тариф" in api.texts(504)[-1] and "23-8" in api.texts(504)[-1]
    await dp.feed_update(bot, cb_update(504, "pf:autook"))
    async with maker() as s:
        u = await s.get(DbUser, 504)
        assert u.quiet_hours == "23-8" and not u.paused
        assert len((await s.scalars(select(UserNegative).where(UserNegative.user_id == 504))).all()) == 4
    await dp.feed_update(bot, cb_update(504, "pf:autook"))  # повтор ничего не дублирует
    async with maker() as s:
        assert len((await s.scalars(select(UserNegative).where(UserNegative.user_id == 504))).all()) == 4
        assert await autosetup.user_plan(s, await s.get(DbUser, 504)) == []
    await dp.feed_update(bot, cb_update(504, "st:auto"))  # чужой не открывает автонастройку бота
    assert not any("Автонастройка бота" in t for t in api.texts(504))
    await dp.feed_update(bot, cb_update(ADMIN_ID, "st:auto"))
    assert "Автонастройка бота" in api.texts(ADMIN_ID)[-1]


async def test_forget_command_asks_confirmation_then_deletes(app_bot, maker):
    from app.models import Lead

    bot, dp, api = app_bot
    async with maker() as s:
        s.add(Lead(author_id=910, username="gone_u", name="Автор", text="Нужен бот", text_hash="forget1"))
        await s.commit()
    await dp.feed_update(bot, text_update(ADMIN_ID, "/forget"))
    assert "Напишите так" in api.texts(ADMIN_ID)[-1]
    await dp.feed_update(bot, text_update(ADMIN_ID, "/forget @gone_u"))
    assert "Удалить данные автора" in api.texts(ADMIN_ID)[-1] and "заявок 1" in api.texts(ADMIN_ID)[-1]
    async with maker() as s:
        assert (await s.scalar(select(Lead.id).where(Lead.author_id == 910))) is not None  # без подтверждения ничего не удалено
    await dp.feed_update(bot, cb_update(ADMIN_ID, "fg:do:910"))
    assert "Удалено" in api.texts(ADMIN_ID)[-1]
    async with maker() as s:
        assert (await s.scalar(select(Lead.id).where(Lead.author_id == 910))) is None
    await dp.feed_update(bot, text_update(ADMIN_ID, "/forget @нет_такого"))
    assert "не найден" in api.texts(ADMIN_ID)[-1]


async def test_forget_is_admin_only_and_never_for_bot_users(app_bot, maker):
    bot, dp, api = app_bot
    await dp.feed_update(bot, text_update(504, "/start"))
    await pass_captcha(bot, dp, 504)
    async with maker() as s:
        (await s.get(DbUser, 504)).onboarded = True
        await s.commit()
    await dp.feed_update(bot, text_update(504, "/forget 905"))
    assert not any("Удалить данные автора" in t or "Напишите так" in t for t in api.texts(504))
    await dp.feed_update(bot, cb_update(504, "fg:do:905"))
    assert not any("Удалено" in t for t in api.texts(504))
    await dp.feed_update(bot, cb_update(ADMIN_ID, "fg:do:504"))  # 504 — пользователь бота: удаляется через /deleteme
    async with maker() as s:
        assert await s.get(DbUser, 504) is not None
