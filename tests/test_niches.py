"""Свои ниши: пользователь предлагает, администратор одобряет, заявки по новой нише приходят."""
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy import select

from app.bot.handlers import categories
from app.bot.handlers import inputs
from app.models import Category, Delivery, Keyword, User, UserCategory
from app.services import intake, niches
from app.services.antispam import AuthorInfo
from app.services.pipeline import Incoming
from tests.conftest import ADMIN_ID, make_user


def test_parse():
    assert niches.parse("Ремонт квартир: ремонт, Отделка,плитка\nнатяжные потолки") == (
        "Ремонт квартир", ["ремонт", "отделка", "плитка", "натяжные потолки"])
    assert niches.parse("Ремонт\nремонт, плитка, обои") == ("Ремонт", ["ремонт", "плитка", "обои"])


def msg(user_id, text):
    m = MagicMock()
    m.from_user.id = user_id
    m.answer = AsyncMock()
    return m


async def propose(maker, uid, text):
    m = msg(uid, text)
    async with maker() as s:
        await inputs.niche_propose(s, m, {}, text)
    return m


async def test_validation(maker, tgbot):
    await make_user(maker, 50)
    for bad, expect in (("Ремонт: ремонт, плитка", "хотя бы"), ("Сайты: сайт, лендинг, магазин", "уже есть"),
                        ("Я: а, б, в", "короткое")):
        m = await propose(maker, 50, bad)
        assert expect in m.answer.call_args.args[0], bad
    async with maker() as s:
        assert await s.scalar(select(Category).where(Category.proposed_by == 50)) is None


async def test_proposal_approve_flow(maker, tgbot):
    await make_user(maker, 50)
    m = await propose(maker, 50, "Ремонт квартир: ремонт, отделка, плитка, натяжные потолки")
    assert "Отправлено администратору" in m.answer.call_args.args[0]
    assert any("Новая ниша" in t for t in tgbot.texts(ADMIN_ID))
    async with maker() as s:
        cat = await s.scalar(select(Category).where(Category.name == "Ремонт квартир"))
        assert cat.pending and not cat.enabled
        assert len((await s.scalars(select(Keyword).where(Keyword.category_id == cat.id))).all()) == 4
    # до одобрения заявок по нише нет
    a = AuthorInfo(user_id=900, username="c", first_name="Али")
    assert await intake.handle(Incoming(text="Ищу кто сделает ремонт квартиры", author=a, group_tg_id=-1,
                                        group_title="Чат", message_id=1)) is None
    c = MagicMock()
    c.data = f"nc:ok:{cat.id}"
    c.answer, c.message.edit_reply_markup = AsyncMock(), AsyncMock()
    c.bot.send_message = AsyncMock()
    await categories.decide_niche(c)
    async with maker() as s:
        cat = await s.get(Category, cat.id)
        assert cat.enabled and not cat.pending
        assert await s.get(UserCategory, (50, cat.id))  # автору ниша подключена
    c.bot.send_message.assert_awaited()
    lead = await intake.handle(Incoming(text="Ищу кто сделает ремонт квартиры срочно", author=AuthorInfo(
        user_id=901, username="c2", first_name="Ва"), group_tg_id=-1, group_title="Чат", message_id=2))
    assert lead is not None
    async with maker() as s:
        assert await s.scalar(select(Delivery).where(Delivery.lead_id == lead.id, Delivery.user_id == 50))


async def test_reject_deletes(maker, tgbot):
    await make_user(maker, 50)
    await propose(maker, 50, "Ремонт квартир: ремонт, отделка, плитка")
    async with maker() as s:
        cat = await s.scalar(select(Category).where(Category.name == "Ремонт квартир"))
    c = MagicMock()
    c.data = f"nc:no:{cat.id}"
    c.answer, c.message.edit_reply_markup = AsyncMock(), AsyncMock()
    c.bot.send_message = AsyncMock()
    await categories.decide_niche(c)
    async with maker() as s:
        assert await s.get(Category, cat.id) is None
        assert not (await s.scalars(select(Keyword).where(Keyword.category_id == cat.id))).all()


async def test_admin_adds_directly_and_daily_limit(maker, tgbot):
    m = await propose(maker, ADMIN_ID, "Ремонт квартир: ремонт, отделка, плитка")
    assert "добавлена" in m.answer.call_args.args[0]
    async with maker() as s:
        cat = await s.scalar(select(Category).where(Category.name == "Ремонт квартир"))
        assert cat.enabled and not cat.pending and await s.get(UserCategory, (ADMIN_ID, cat.id))
    await make_user(maker, 50)
    for i in range(3):
        await propose(maker, 50, f"Ниша номер {i}: слово, другое, третье")
    m = await propose(maker, 50, "Ещё ниша: слово, другое, третье")
    assert "лимит" in m.answer.call_args.args[0]
