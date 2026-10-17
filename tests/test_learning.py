"""Фильтр учится на отметках админа; отчёт о проблемах собирается без личных данных."""
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy import select

from app.models import Delivery, ErrorLog, LabeledText, Lead, User
from app.services import diagnostics, intake, lead_actions, offers
from app.services.antispam import AuthorInfo
from app.services.pipeline import Incoming
from tests.conftest import ADMIN_ID, make_user

ODD = "Добрый день всем участникам чата, помогаю предпринимателям, нужен сайт для магазина — обсудим вашу задачу"


def msg(text, author):
    return Incoming(text=text, author=AuthorInfo(user_id=author, username="u", first_name="И"),
                    group_tg_id=-100800, group_title="Чат", message_id=author)


async def test_admin_marks_offer_and_similar_is_filtered(maker, tgbot):
    await make_user(maker, 60, plan="full")
    lead = await intake.handle(msg(ODD, 9201))
    assert lead is not None  # правила не узнали — пришло как заявка
    async with maker() as s:
        admin = await s.get(User, ADMIN_ID)
        d = await s.scalar(select(Delivery).where(Delivery.lead_id == lead.id, Delivery.user_id == ADMIN_ID))
        note = await lead_actions.apply_status(s, admin, d, lead, "offer")
        await s.commit()
        assert "Исполнитель" in note and await s.scalar(select(LabeledText))
    # тот же автор-исполнитель с чуть другим текстом и с другого аккаунта — уже отсеян
    assert await intake.handle(msg(ODD.replace("Добрый день", "Здравствуйте"), 9202)) is None
    async with maker() as s:
        assert (await offers.verdict(s, ODD))[1] == "ваши отметки"


async def test_report_without_private_data(maker, tgbot):
    from app.bot import notify

    await notify.alert_once("test_key", "Ошибка у @someone, телефон +7 900 123 45 67")
    async with maker() as s:
        s.add(LabeledText(kind="request", text="Ищу дизайнера, пишите @ivan_petrov или +79001234567, сайт t.me/xxx"))
        await s.commit()
        assert await s.scalar(select(ErrorLog))
        text = await diagnostics.report(s)
    assert "СБОИ" in text and "test_key" in text and "ivan_petrov" not in text and "9001234567" not in text and "someone" not in text


async def test_report_button(maker, tgbot):
    from app.bot.handlers import settings as st

    c = MagicMock()
    c.answer, c.message.answer, c.message.answer_document = AsyncMock(), AsyncMock(), AsyncMock()
    await st.diagnostics_report(c)
    assert c.message.answer_document.await_count == 1
