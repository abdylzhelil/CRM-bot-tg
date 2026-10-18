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


ADS = ["Делаем сайты под ключ, портфолио в профиле, обращайтесь в лс",
       "Позволь пригласить тебя во флуд, присоединяйтесь, нас пока мало, заходите",
       "Баннеры по низкой цене, вы сможете заказать у нас прямо сейчас #баннер #ава #постер"]


def test_content_reason():
    assert junk.content_reason(ADS * 4).startswith("почти одна реклама")
    assert junk.content_reason(["Hello everyone, welcome to our nice friendly chat"] * 12).startswith("сообщения не на русском")
    normal = ["Ищу мастера по ремонту стиральной машины, кто может завтра", "Продам детскую коляску почти новую недорого",
              "Подскажите хорошего стоматолога в нашем районе пожалуйста"] * 4
    assert junk.content_reason(normal) == ""
    assert junk.content_reason(ADS) == ""  # мало сообщений — не судим


class Msg:
    def __init__(self, text):
        from datetime import datetime

        self.message, self.date = text, datetime(2026, 10, 18)


class QualityClient:
    def __init__(self, texts):
        self.texts, self.joined = texts, False

    async def get_entity(self, _):
        return object()

    async def iter_messages(self, *_a, **_k):
        for t in self.texts:
            yield Msg(t)

    async def __call__(self, req):
        self.joined = True

    def is_connected(self):
        return True


async def test_scout_does_not_join_junk_and_admin_can_return(maker, monkeypatch):
    from app.bot.handlers import groups as groups_h
    from app.models import ScoutAccount
    from app.scout import client as scout_client, joiner

    monkeypatch.setattr(scout_client, "ensure_connected", AsyncMock(return_value=True))
    async with maker() as s:
        s.add(Group(tg_id=-901, username="g901", title="Чат района", status="approved", members=500))
        s.add(ScoutAccount(id=5, name="sc5", api_id=1, api_hash="x", daily_join_limit=10))
        await s.commit()
    client = QualityClient(ADS * 4)
    assert await joiner.join_one(client, 5) is True
    assert not client.joined
    async with maker() as s:
        g = await s.scalar(select(Group).where(Group.tg_id == -901))
        acc = await s.get(ScoutAccount, 5)
    assert g.status == "skipped" and "реклама" in g.skip_reason and acc.joined_today == 0
    # админ вернул вручную — вступает без проверки
    c = MagicMock()
    c.data, c.answer, c.message.edit_text = f"gr:back:{g.id}", AsyncMock(), AsyncMock()
    c.message.answer = AsyncMock()
    await groups_h.back_to_queue(c)
    monkeypatch.setattr("app.scout.folders.mute", AsyncMock())
    monkeypatch.setattr("app.scout.folders.sync_folders", AsyncMock())
    assert await joiner.join_one(client, 5) is True
    assert client.joined


async def test_queue_cleanup_by_title(session):  # noqa: F811
    from app.models import Setting as S

    await session.delete(await session.get(S, junk.QUEUE_KEY))
    session.add_all([Group(tg_id=31, title="Взаимопиар чат", status="approved"),
                     Group(tg_id=32, title="Ремонт Казань", status="approved")])
    await session.commit()
    await junk.mark_existing(session)
    st = {g.tg_id: g.status for g in (await session.scalars(select(Group).where(Group.tg_id.in_((31, 32))))).all()}
    assert st == {31: "skipped", 32: "approved"}
