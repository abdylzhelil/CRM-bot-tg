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


async def test_offer_votes_from_users_teach_filter(maker, tgbot):
    """Обычный пользователь: «🙅 Исполнитель» скрывает заявку у него; фильтр учится после 2 голосов разных людей."""
    from sqlalchemy import select

    from app.models import Delivery, LabeledText, User

    text = "Делаю сайты на тильде под ключ, примеры работ в профиле, пишите в лс"
    async with maker() as s:
        lead = Lead(text_hash="o1", text=text, group_title="G", author_id=99)
        s.add(lead)
        await s.flush()
        ds = []
        for uid in (301, 302, 303):
            s.add(User(tg_id=uid, name=f"u{uid}", onboarded=True))
            d = Delivery(lead_id=lead.id, user_id=uid, sent=uid != 303)
            s.add(d)
            ds.append(d)
        await s.commit()
        users = [await s.get(User, uid) for uid in (301, 302)]
        note = await lead_actions.apply_status(s, users[0], ds[0], lead, "offer")
        await s.commit()
        assert "1 из 2" in note and ds[0].status == "rejected"
        assert not await s.scalar(select(LabeledText))
        # передумал — голос снимается
        await lead_actions.restore(s, users[0], ds[0], lead)
        await s.commit()
        await lead_actions.apply_status(s, users[0], ds[0], lead, "offer")
        note = await lead_actions.apply_status(s, users[1], ds[1], lead, "offer")
        await s.commit()
        assert "похожие больше не придут" in note
        assert (await s.scalar(select(LabeledText))).kind == "offer"
        assert (await s.get(Delivery, ds[2].id)).status == "rejected"  # неотправленная копия снята
