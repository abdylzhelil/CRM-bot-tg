"""«Навести порядок»: места пользователей → какие группы остаются, выход по кнопке, остановка поиска, места разведчика."""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy import select

from app.models import Group, ScoutAccount, User, now
from app.scout import joiner, publisher, search
from app.services import geo, settings, tidy


def test_place_state():
    a = ["Дагестан"]
    assert geo.place_state("", "", "Барахолка Дербент", a) is True
    assert geo.place_state("Каспийск", "", "x", a) is True  # город разрешённого региона
    assert geo.place_state("", "", "Паттайя Барахолка №1 🇹🇭", a) is False  # заграница
    assert geo.place_state("", "", "Барахолка Москва", a) is False
    assert geo.place_state("", "", "Работа Услуги", a) is None  # место не определить — решит проверка после вступления
    assert geo.place_state("", "Дагестан", "x", ["Махачкала"]) is True  # группа всего региона подходит городу
    assert geo.place_state("Москва", "", "x", []) is True  # ограничения нет
    assert geo.parse_places("Дагестан, Луна, Москва") == ["Дагестан", "Москва"]


async def _world(maker):
    async with maker() as s:
        for i, city in enumerate(["Махачкала", "Махачкала", "Дербент", ""]):
            s.add(User(tg_id=500 + i, name=f"u{i}", onboarded=True, city=city))
        long_ago = now() - timedelta(days=40)
        s.add_all([
            Group(tg_id=-1, title="Барахолка Махачкала", status="joined", city="Махачкала", leads_count=5, joined_at=long_ago),
            Group(tg_id=-2, title="Реклама и пиар чат", status="joined", leads_count=1, joined_at=long_ago),
            Group(tg_id=-3, title="Паттайя Барахолка №1 🇹🇭", status="joined", leads_count=2, joined_at=long_ago),
            Group(tg_id=-4, title="Работа Дербент", status="joined", region="Дагестан", leads_count=0, joined_at=long_ago),
            Group(tg_id=-5, title="Барахолка Москва", status="joined", city="Москва", leads_count=0, joined_at=long_ago,
                  catalog=True, submitted_by=500),  # защищена: группа из каталога
            Group(tg_id=-6, title="Объявления", status="joined", leads_count=0, joined_at=now()),  # свежая — ещё рано судить
        ])
        await s.commit()


async def test_user_places_and_classify(maker):
    await _world(maker)
    async with maker() as s:
        users = await tidy.user_places(s)
        assert dict(users)["Махачкала"] == 2 and dict(users)["Дагестан"] == 3 and dict(users)["Дербент"] == 1
        assert "Москва" not in dict(users)  # «вся Россия» (пусто) ограничением не считаем
        await settings.put(s, "scout_places", ", ".join(tidy.allowed_default(users, "Дагестан")))
        c = await tidy.classify(s)
    assert [g.tg_id for g in c["spam"]] == [-2]
    assert [g.tg_id for g in c["place"]] == [-3]  # Паттайя — чужое место
    assert [g.tg_id for g in c["quiet"]] == [-4]  # Дербент подходит по месту, но заявок 40 дней ноль
    assert c["protected"] == 1 and c["keep"] == 2  # Махачкала с заявками и свежая «Объявления»


async def test_leave_groups_by_button(maker, tgbot, monkeypatch):
    await _world(maker)
    async with maker() as s:
        s.add(ScoutAccount(id=1, name="sc", api_id=1, api_hash="x", enabled=True, status="ok"))
        for g in (await s.scalars(select(Group))).all():
            g.account_id = 1
        await s.commit()
    client = SimpleNamespace(calls=[])

    async def call(req):
        client.calls.append(type(req).__name__)

    client.__call__ = call
    fake = MagicMock()
    fake.side_effect = lambda req: call(req)
    monkeypatch.setattr(publisher, "resolve", AsyncMock(return_value="e"))
    monkeypatch.setattr(publisher.asyncio, "sleep", AsyncMock())

    class C:
        async def __call__(self, req):
            client.calls.append(type(req).__name__)

    manager = SimpleNamespace(clients={1: C()})
    async with maker() as s:
        ids = [g.id for g in await s.scalars(select(Group).where(Group.tg_id.in_([-2, -5])))]
    res = await publisher.leave_groups(manager, ids, "порядок: спам")
    assert res == {"left": 1, "failed": 0, "skipped": 1}  # группа из каталога защищена
    async with maker() as s:
        g2 = await s.scalar(select(Group).where(Group.tg_id == -2))
        g5 = await s.scalar(select(Group).where(Group.tg_id == -5))
        assert (g2.status, g2.skip_reason) == ("left", "порядок: спам") and g5.status == "joined"


async def test_stop_growth_and_places_filter(maker, tgbot, monkeypatch):
    async with maker() as s:
        s.add(ScoutAccount(id=1, name="sc", api_id=1, api_hash="x", enabled=True, status="ok"))
        s.add(Group(tg_id=-7, title="G", username="g", status="approved", kind="group"))
        await settings.put(s, "scout_grow", "0")
        await s.commit()

    class C:
        def is_connected(self):
            return True

    assert await joiner.join_one(C(), 1) is False  # вступление остановлено
    sent = AsyncMock()
    monkeypatch.setattr(search.notify, "_send", sent)
    assert await search.discover(C(), report=True) == 0
    assert "выключен" in sent.call_args.args[0]
    async with maker() as s:
        assert (await s.scalar(select(Group).where(Group.tg_id == -7))).status == "approved"  # группа не тронута
        assert settings.validate("scout_places", "Дагестан, Москва") == "Дагестан, Москва"
        try:
            settings.validate("scout_places", "Луна")
            raise AssertionError("должно быть отказано")
        except settings.SettingError:
            pass


async def test_places_and_tidy_screens(maker, tgbot):
    from app.bot.handlers import groups as groups_h

    await _world(maker)
    c = MagicMock()
    c.data, c.answer = "gp:users", AsyncMock()
    c.message.edit_text, c.message.answer = AsyncMock(), AsyncMock()
    await groups_h.gp_users(c)
    async with maker() as s:
        assert "Дагестан" in await settings.get(s, "scout_places")
    c.data = "gt:menu"
    await groups_h.gt_menu(c)
    text = c.answer.call_args.args[0]  # у MagicMock reply() отвечает через .answer
    assert "Навести порядок" in text and "Спамные" in text and "Чужие места" in text
