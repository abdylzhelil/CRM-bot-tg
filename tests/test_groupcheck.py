"""🧹 Проверка групп: спам/заработок не берём, из спамных уходим, охват — город/регион/вся страна."""
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import select
from telethon import functions

from app import config
from app.models import Group, ScoutAccount
from app.services import geo, groupcheck, llm

EARN = [
    "Нужны люди для удалённой работы, доход от 3000 в день #онлайнзаработок #работавлюбомгороде",
    "Ищу партнёров, пассивный доход без вложений, пишите в личку",
    "Крипта растёт, схема заработка на p2p, подключайтесь сегодня",
    "Подработка для всех, оплата от 2000 в день, обучение бесплатно",
]
LIVE = [
    "Подскажите хорошего сантехника в Каспийске, течёт кран на кухне",
    "Отдам детскую коляску в хорошем состоянии, самовывоз",
    "Кто знает, где сделать ремонт телефона недорого и быстро",
    "Ищу репетитора по математике для сына, 7 класс",
]


def test_earn_spam_by_rules():
    assert groupcheck.earn_reason(EARN * 3).startswith("заработок")
    assert groupcheck.earn_reason(LIVE * 3) == ""
    assert groupcheck.by_rules("Работа онлайн", "", EARN * 3)["verdict"] == "spam"
    assert groupcheck.by_rules("Барахолка", "", LIVE * 3)["verdict"] == "good"


def test_place():
    assert groupcheck.place("Барахолка Махачкала") == {"scope": "city", "city": "Махачкала", "region": "Дагестан"}
    assert groupcheck.place("Объявления Дагестан")["scope"] == "region"
    assert groupcheck.place("Услуги по всей России")["scope"] == "country"
    assert groupcheck.place("Чат соседей")["scope"] == ""
    assert geo.detect("нужен мастер", "Чат соседей", "", "Тверская область") == ("", "Тверская область")
    assert geo.detect("нужен мастер", "Чат", "Тверь", "Тверская область") == ("Тверь", "Тверская область")


def _ai(monkeypatch, answer: str):
    monkeypatch.setattr(config, "GIGACHAT_AUTH_KEY", "g")

    async def fake_complete(provider, system, text, max_tokens=400, json_mode=False):
        return answer
    monkeypatch.setattr(llm, "complete", fake_complete)


async def test_ai_fills_scope_and_catches_spam(maker, monkeypatch):
    _ai(monkeypatch, '{"verdict": "good", "scope": "city", "city": "Тверь", "region": "Тверская область", "reason": "живая"}')
    async with maker() as s:
        res = await groupcheck.assess(s, "Чат соседей", "", LIVE * 3)
    assert (res["verdict"], res["scope"], res["city"], res["region"]) == ("good", "city", "Тверь", "Тверская область")
    _ai(monkeypatch, '{"verdict": "spam", "scope": "country", "city": "", "region": "", "reason": "одна реклама"}')
    async with maker() as s:
        res = await groupcheck.assess(s, "Чат соседей", "", LIVE * 3)
    assert res["verdict"] == "spam" and res["reason"].startswith("ИИ:")


class Client:
    def __init__(self, texts):
        self.texts, self.calls = texts, []

    async def __call__(self, req):
        self.calls.append(type(req).__name__)
        return SimpleNamespace(full_chat=SimpleNamespace(about=""))

    async def iter_messages(self, entity, limit=100):
        base = datetime(2026, 10, 24, 12)
        for i, t in enumerate(self.texts):
            yield SimpleNamespace(message=t, date=base - timedelta(hours=i))


async def test_audit_leaves_spam_but_keeps_protected(maker, monkeypatch):
    from app.scout import publisher

    monkeypatch.setattr(publisher, "resolve", AsyncMock(return_value=object()))
    monkeypatch.setattr(publisher.asyncio, "sleep", AsyncMock())
    async with maker() as s:
        s.add(ScoutAccount(id=1, name="sc", api_id=1, api_hash="x"))
        await s.flush()
        s.add_all([Group(tg_id=-801, title="Работа онлайн", status="joined", account_id=1, members=900),
                   Group(tg_id=-802, title="Заработок каталог", status="joined", account_id=1, members=800, catalog=True),
                   Group(tg_id=-803, title="Барахолка Махачкала", status="joined", account_id=1, members=700)])
        await s.commit()
    spam, live = Client(EARN * 3), Client(LIVE * 3)
    manager = SimpleNamespace(clients={1: spam})
    out = await publisher.audit_round(manager)
    assert out == {"left": 2, "muted": 1}  # все три читаются как «заработок»; каталог не покидаем
    async with maker() as s:
        g1 = await s.scalar(select(Group).where(Group.tg_id == -801))
        g2 = await s.scalar(select(Group).where(Group.tg_id == -802))
        g3 = await s.scalar(select(Group).where(Group.tg_id == -803))
    assert g1.status == "left" and g1.skip_reason.startswith("проверка: заработок")
    assert g2.status == "joined" and g2.no_leads  # каталог не покидаем, только не берём заявки
    assert functions.channels.LeaveChannelRequest.__name__ in spam.calls
    # третью вернули и перепроверили уже с живыми сообщениями — остаётся, охват определён
    manager.clients[1] = live
    async with maker() as s:
        g3 = await s.get(Group, g3.id)
        g3.audit_at, g3.status, g3.no_leads, g3.skip_reason = None, "joined", False, ""
        await s.commit()
    assert await publisher.audit_one(manager, g3.id) == "good"
    async with maker() as s:
        g3 = await s.get(Group, g3.id)
    assert (g3.scope, g3.city, g3.region, g3.status) == ("city", "Махачкала", "Дагестан", "joined")
    async with maker() as s:  # проверенные раз в месяц — сразу снова не берутся
        assert await groupcheck.due(s, {1}) == []


async def test_audit_off_leave_only_mutes(maker, monkeypatch):
    from app.scout import publisher
    from app.services import settings

    monkeypatch.setattr(publisher, "resolve", AsyncMock(return_value=object()))
    async with maker() as s:
        await settings.put(s, "group_audit_leave", "0")
        s.add(ScoutAccount(id=1, name="sc", api_id=1, api_hash="x"))
        await s.flush()
        s.add(Group(tg_id=-811, title="Работа онлайн", status="joined", account_id=1))
        await s.commit()
        gid = (await s.scalar(select(Group).where(Group.tg_id == -811))).id
    assert await publisher.audit_one(SimpleNamespace(clients={1: Client(EARN * 3)}), gid) == "muted"


async def test_joiner_quality_skips_earning_group(maker):
    from app.scout import joiner

    g = Group(tg_id=-821, title="Чат объявлений")
    reason, res = await joiner._quality(Client(EARN * 3), object(), g)
    assert reason.startswith("заработок") and res["verdict"] == "spam"
    reason, res = await joiner._quality(Client(LIVE * 3), object(), Group(tg_id=-822, title="Барахолка Дербент"))
    assert reason == "" and res["city"] == "Дербент"


async def test_toggles_in_sections_and_settings_split(maker):
    """Владелец не нашёл «Читать правила групп» среди 29 настроек: реклама — отдельным разделом, кнопки в самих разделах."""
    from unittest.mock import MagicMock

    from app.bot.handlers import groups as groups_h
    from app.services import settings

    scout = next(keys for _, title, keys in settings.SECTIONS if title == "Разведчик")
    ads = next(keys for _, title, keys in settings.SECTIONS if title == "Реклама в группах")
    assert "group_rules_check" in scout and "group_audit" in scout and len(scout) <= 16
    assert all(k.startswith("adpost_") for k in ads)
    cb = MagicMock()
    cb.data = "gr:tg:group_rules_check"
    cb.answer = AsyncMock()
    await groups_h.toggle(cb)
    async with maker() as s:
        assert await settings.get(s, "group_rules_check") == "0"
    markup = cb.answer.call_args.kwargs["reply_markup"]  # MagicMock не CallbackQuery — reply отвечает новым сообщением
    assert any("Включить чтение правил" in b.text for row in markup.inline_keyboard for b in row)
