"""💬 Ответы пользователей (например, на рассылку) не теряются: приходят админу, админ отвечает из бота."""
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy import select

from app.models import Feedback, User
from tests.conftest import ADMIN_ID, make_user


def message(uid, text):
    m = MagicMock()
    m.text, m.from_user.id = text, uid
    m.answer = AsyncMock()
    return m


async def test_free_text_reaches_admin_and_admin_replies(maker, tgbot):
    from app.bot import setup
    from app.bot.handlers import feedback as fb_h
    from app.bot.handlers import inputs

    user = await make_user(maker, 80)
    await setup.unknown(message(80, "Приходят не те заявки, нужен только ремонт"), user)
    async with maker() as s:
        fb = await s.scalar(select(Feedback))
    assert fb.user_id == 80 and "не те заявки" in fb.text and not fb.read
    assert any("не те заявки" in t for t in tgbot.texts(ADMIN_ID))  # админу пришло сразу
    # админ видит список и отвечает
    admin = await make_user(maker, 81, is_admin=True)
    c = MagicMock()
    c.answer, c.message.answer = AsyncMock(), AsyncMock()
    await fb_h.show_list(c, unread_only=True)
    assert "новых: 1" in c.answer.call_args_list[0].args[0]  # сначала заголовок, потом карточки
    async with maker() as s:
        await inputs.HANDLERS["fb_reply"](s, message(81, "x"), {"fid": fb.id}, "Спасибо, поправим ниши!")
    assert any("поправим ниши" in t for t in tgbot.texts(80))
    async with maker() as s:
        fb = await s.get(Feedback, fb.id)
    assert fb.read and fb.answer.startswith("Спасибо")
    _ = admin


async def test_admin_text_and_commands_are_not_feedback(maker, tgbot):
    from app.bot import setup

    admin = await make_user(maker, 82, is_admin=True)
    await setup.unknown(message(82, "просто текст"), admin)
    user = await make_user(maker, 83)
    await setup.unknown(message(83, "ы"), user)  # слишком коротко — просто меню
    async with maker() as s:
        assert await s.scalar(select(Feedback)) is None


async def test_write_button_and_deleteme(maker, tgbot):
    from app.bot.handlers import inputs
    from app.services import erase

    await make_user(maker, 84)
    async with maker() as s:
        assert await inputs.HANDLERS["feedback"](s, message(84, "x"), {}, "Хочу нишу «Пчеловодство»") is None
        assert await s.scalar(select(Feedback.user_id)) == 84
    async with maker() as s:
        assert await erase.erase_user(s, 84)
        await s.commit()
        assert await s.scalar(select(Feedback)) is None  # /deleteme удаляет и сообщения
        assert await s.get(User, 84) is None
