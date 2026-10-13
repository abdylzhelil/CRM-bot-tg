"""Сквозной сценарий через диспетчер aiogram с фальшивым Telegram: регистрация, меню, права."""
from datetime import datetime
from itertools import count

import pytest
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, EditMessageText, GetMe, SendMessage, TelegramMethod
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
