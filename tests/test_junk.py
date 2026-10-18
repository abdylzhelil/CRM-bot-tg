"""Мусор из групп рекламы/флуда: приглашения во флуд, реклама баннеров, группы «без заявок»."""
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy import select

from app.models import Group, Setting
from app.services import junk, offers
from app.services.antispam import AuthorInfo
from app.services.pipeline import Incoming, process
from tests.test_pipeline import session  # noqa: F401  (фикстура)

FLOOD = ("приветик всем, я хотела позвать вас в фулд по игре Genshin Impact. Нас пока не много, но именно ты можешь "
         "стать тем кто поможет мне. Буду рада если присоединитесь.")
FLOOD2 = ("Здравствуй! Позволь пригласить тебя в только что открывшийся флуд по аниме. Почему стоит вступить к нам? "
          "Мы находимся в активном поиске участников")
BANNER = ("СТОЙ!!! Хочешь, чтобы все было по красоте? Не умеешь делать баннеры? Не беда! В shop monsi вы сможете "
          "заказать себе баннеры по низкой цене. НИЗКИЕ ЦЕНЫ!!! #баннер #ава #постер #banner")


def test_rules_catch_flood_and_banner_ads():
    assert offers.rule_verdict(FLOOD) == "other"
    assert offers.rule_verdict(FLOOD2) == "other"
    assert offers.rule_verdict(BANNER) == "offer"
    # настоящие просьбы — по-прежнему заявки
    assert offers.rule_verdict("Ищу дизайнера сделать баннер для группы, бюджет 1000") == "request"
    assert offers.rule_verdict("Подскажите, где можно заказать торт на день рождения") == "request"
    assert offers.rule_verdict("Хочу заказать сайт, кто сделает? Заходите в личку") == "request"


def test_junk_titles():
    assert junk.is_junk_title("Реклама ролок/флудов🛸")
    assert junk.is_junk_title("Взаимопиар | PR чат")
    assert not junk.is_junk_title("Барахолка Москва")
    assert not junk.is_junk_title("Работа в Мытищах | Вакансии")


async def test_no_leads_group_gives_nothing(session):  # noqa: F811
    session.add(Group(tg_id=10, title="Бизнес", status="joined", no_leads=True))
    await session.commit()
    a = AuthorInfo(user_id=7, username="u7", first_name="Иван")
    text = "Ищу разработчика для лендинга, бюджет 10к"
    assert await process(session, Incoming(text=text, author=a, group_tg_id=10, group_title="Бизнес", message_id=1)) is None
    # админ вернул вручную («↩️ Это заявка») — проходит
    assert await process(session, Incoming(text=text, author=a, group_tg_id=10, group_title="Бизнес", message_id=1),
                         force=True)


async def test_mark_existing_once(session):  # noqa: F811
    await session.delete(await session.get(Setting, junk.KEY))
    session.add_all([Group(tg_id=1, title="Реклама ролок", status="joined"),
                     Group(tg_id=2, title="Барахолка", status="joined")])
    await session.commit()
    assert await junk.mark_existing(session) == 1
    g = await session.scalar(select(Group).where(Group.tg_id == 1))
    g.no_leads = False  # админ вернул — второй раз не трогаем
    await session.commit()
    assert await junk.mark_existing(session) == 0
    assert not (await session.scalar(select(Group).where(Group.tg_id == 1))).no_leads


async def test_admin_button_turns_group_off_and_back(maker, tgbot):
    from app.bot.handlers import leads as leads_h
    from app.models import Delivery, Lead
    from tests.conftest import make_user

    admin = await make_user(maker, 4242, is_admin=True)
    async with maker() as s:
        g = Group(tg_id=-1005, title="Реклама ролок", status="joined")
        s.add(g)
        await s.flush()
        lead = Lead(author_id=9, text="мусор", group_id=g.id, group_title=g.title, message_id=1, text_hash="h")
        s.add(lead)
        await s.flush()
        d = Delivery(lead_id=lead.id, user_id=4242, sent=True)
        s.add(d)
        await s.commit()
        gid, did = g.id, d.id
    for action, want in (("nogrp", True), ("yesgrp", False)):
        c = MagicMock()
        c.data = f"lead:{action}:{did}"
        c.answer, c.message.edit_reply_markup = AsyncMock(), AsyncMock()
        await leads_h.lead_action(c, admin)
        async with maker() as s:
            assert (await s.get(Group, gid)).no_leads is want
