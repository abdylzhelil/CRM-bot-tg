"""Спам с разных аккаунтов и «серые» темы: один раз нажали «🚫 Спам» — больше не приходит."""
from sqlalchemy import select

from app.models import BlacklistEntry, Delivery, SpamSample, User
from app.services import intake, lead_actions, spamtexts
from app.services.antispam import AuthorInfo, score_message
from app.services.pipeline import Incoming
from tests.conftest import ADMIN_ID, make_user

SPAM = "Нужен сайт? Сделаем лендинг под ключ всего за 3 дня, пишите в личку, скидка только сегодня для всех желающих"
SPAM2 = "Нужен сайт?? Сделаем лендинг под ключ всего за 2 дня! Пишите в личку — скидка только сегодня для всех желающих 🔥"


def msg(text, author, group=-100500):
    return Incoming(text=text, author=AuthorInfo(user_id=author, username=f"u{author}", first_name="Али"),
                    group_tg_id=group, group_title="Бизнес чат", message_id=author)


def test_similar_texts_and_short_ones():
    assert spamtexts.similar(spamtexts.words(SPAM), spamtexts.words(SPAM2))  # чуть изменили — всё равно похоже
    assert not spamtexts.similar(spamtexts.words("нужен сайт"), spamtexts.words("нужен сайт"))  # короткие не сравниваем
    assert not spamtexts.similar(spamtexts.words(SPAM), spamtexts.words(
        "Ищу дизайнера для интернет магазина одежды, бюджет обсуждаем, нужен опыт работы с маркетплейсами"))


def test_grey_topics_score_high():
    a = AuthorInfo(user_id=123, username="x", first_name="Иван")
    assert "серая тема" in score_message(a, "Сделаю права без экзаменов, купить права быстро").reasons
    assert "серая тема" in score_message(a, "Работа для всех, доход от 5000 ₽ в день").reasons
    assert "серая тема" not in score_message(a, "Нужен сайт для салона красоты, бюджет 30 тысяч").reasons


async def test_admin_spam_blocks_same_text_from_other_accounts(maker, tgbot):
    await make_user(maker, 60, plan="full")
    lead = await intake.handle(msg(SPAM, 7001))
    assert lead is not None
    async with maker() as s:
        admin = await s.get(User, ADMIN_ID)
        d = await s.scalar(select(Delivery).where(Delivery.lead_id == lead.id, Delivery.user_id == ADMIN_ID))
        note = await lead_actions.apply_status(s, admin, d, lead, "spam")
        await s.commit()
        assert "похожие" in note and await spamtexts.count(s) == 1
    # тот же спам с другого аккаунта и в другой группе — уже не заявка, и аккаунт тоже в чёрном списке
    assert await intake.handle(msg(SPAM2, 7002, group=-100501)) is None
    async with maker() as s:
        assert (await s.get(BlacklistEntry, 7002)).status == "spam"
        assert (await s.scalar(select(SpamSample))).hits == 1
    # передумали: вернули заявку — образец удаляется, похожие снова доходят
    async with maker() as s:
        admin = await s.get(User, ADMIN_ID)
        d = await s.scalar(select(Delivery).where(Delivery.lead_id == lead.id, Delivery.user_id == ADMIN_ID))
        await lead_actions.apply_status(s, admin, d, lead, "restore")
        await s.commit()
        assert await spamtexts.count(s) == 0
    assert await intake.handle(msg(SPAM2 + " и ещё", 7003, group=-100502)) is not None


async def test_spam_mark_also_hides_other_messages_of_same_author(maker, tgbot):
    """Автор успел написать два разных сообщения — одного нажатия «спам» хватает на оба."""
    await make_user(maker, 61, plan="full")
    first = await intake.handle(msg("Нужен сайт для магазина одежды, кто сделает недорого?", 7010))
    second = await intake.handle(msg("Кто сделает лендинг для кофейни? Нужен сайт срочно", 7010, group=-100503))
    assert first and second
    async with maker() as s:
        user = await s.get(User, 61)
        d1 = await s.scalar(select(Delivery).where(Delivery.lead_id == first.id, Delivery.user_id == 61))
        d2 = await s.scalar(select(Delivery).where(Delivery.lead_id == second.id, Delivery.user_id == 61))
        assert d1 and d2
        await lead_actions.apply_status(s, user, d1, first, "spam")
        await s.commit()
        assert (await s.get(Delivery, d2.id)).status == "spam"


async def test_admin_adds_sample_by_hand(maker, tgbot):
    from unittest.mock import AsyncMock, MagicMock

    from app.bot.handlers import blacklist

    m = MagicMock()
    m.from_user.id = ADMIN_ID
    m.answer = AsyncMock()
    async with maker() as s:
        await blacklist.bl_sample(s, m, {}, SPAM)
        await blacklist.bl_sample(s, m, {}, "коротко")
    async with maker() as s:
        assert await spamtexts.count(s) == 1
