"""Поиск групп не выдыхается (запросы из ниш и городов по кругу) и понятно, почему разведчик не вступает."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import select

from app.models import Group, ScoutAccount, Setting
from app.scout import search
from app.services import scoutdiag, settings


async def test_queries_from_niches_and_cities_rotate(maker):
    async with maker() as s:
        all_q = await search.build_queries(s)
        assert len(all_q) > 100  # не 11 ручных, а сотни
        assert "барахолка Махачкала" in all_q and any(q.startswith("сантехник") or q == "сайт" for q in all_q)
        assert len({q.casefold() for q in all_q}) == len(all_q)  # без повторов
        home_q, near_q, rest = await search.tier_queries(s)
        assert len(home_q) + len(near_q) + len(rest) == len(all_q) and all_q[0] == "Махачкала"
        assert any("Дербент" in q for q in home_q) and not any("Москва" in q for q in home_q)
        assert any("Грозный" in q for q in near_q) and any("Пятигорск" in q for q in near_q)
        assert not any("Москва" in q for q in near_q) and any("Москва" in q for q in rest)
        # этап 0 — только свой регион, пока его круг не пройден
        b1, st1, total = await search.next_batch(s)
        assert st1 == 0 and len(b1) == search.PER_PASS and all(q in home_q for q in b1)
        assert total == len(all_q)
        for _ in range(len(home_q) // search.PER_PASS + 1):
            b, st, _ = await search.next_batch(s)
        assert await search.stage(s) >= 1
        # этап 1 — свой регион + Кавказ
        await settings.put(s, "search_stage", "1")
        b, st, _ = await search.next_batch(s)
        assert st == 1 and sum(q in near_q for q in b) == 14 and sum(q in home_q for q in b) == 6
        await settings.put(s, "search_stage", "2")
        b, st, _ = await search.next_batch(s)
        assert st == 2 and sum(q in rest for q in b) == 10


async def test_rewind_returns_undone_queries_to_their_circle(maker):
    async with maker() as s:
        await settings.put(s, "search_stage", "2")
        b, _, _ = await search.next_batch(s)
        before = {k: await settings.raw(s, k) for k in search.TIER_KEYS}
    await search.rewind_tiers(2)  # сделали 2 из 4 «своих», остальное не успели
    async with maker() as s:
        tiers = await search.tier_queries(s)
        after = {k: await settings.raw(s, k) for k in search.TIER_KEYS}
    home_key, near_key, rest_key = search.TIER_KEYS
    assert int(after[home_key]) == (int(before[home_key]) - 2) % len(tiers[0])
    assert int(after[near_key]) == (int(before[near_key]) - 6) % len(tiers[1])
    assert int(after[rest_key]) == (int(before[rest_key]) - 10) % len(tiers[2])


async def test_discover_reports_even_when_nothing_new(maker, tgbot, monkeypatch):
    sent = AsyncMock()
    monkeypatch.setattr(search.notify, "_send", sent)
    monkeypatch.setattr(search.asyncio, "sleep", AsyncMock())
    async with maker() as s:
        s.add(Group(tg_id=-1001, title="Старая", status="joined"))
        await s.commit()
    chat = SimpleNamespace(megagroup=True, id=1, title="Старая", username="old")
    monkeypatch.setattr(search, "Channel", SimpleNamespace)
    monkeypatch.setattr(search.utils, "get_peer_id", lambda c: -1001)

    client = FakeClient()
    client.__class__ = type("C", (FakeClient,), {"__call__": lambda self, req: _chats(chat)})
    assert await search.discover(client, report=True) == 0
    assert "Новых групп нет" in sent.call_args.args[0] and "уже знали" in sent.call_args.args[0]
    assert "новых 0" in await search.last_search()


async def test_diagnosis_explains_why_not_joining(maker):
    async with maker() as s:
        s.add(ScoutAccount(id=1, name="sc", api_id=1, api_hash="x", enabled=True, status="ok"))
        s.add(Group(tg_id=-5, title="Кандидат", status="candidate", kind="group"))
        await s.commit()
        level, lines = await scoutdiag.join_diagnosis(s, SimpleNamespace(clients={1: object()}))
        assert level == "warn" and any("ждут вашего отбора" in x for x in lines)
        await settings.put(s, "scout_paused", "1")
        level, lines = await scoutdiag.join_diagnosis(s, SimpleNamespace(clients={}))
        assert level == "bad" and any("на паузе" in x for x in lines) and any("НЕ ПОДКЛЮЧЁН" in x for x in lines)
        assert await s.get(Setting, "search_offset") is None


class FakeClient:
    """Клиент Telethon, у которого посреди поиска обрывается связь."""

    def __init__(self, connected=True, drop_after=None, reconnect=False):
        self.connected, self.calls, self.drop_after, self.reconnect = connected, 0, drop_after, reconnect

    def is_connected(self):
        return self.connected

    async def connect(self):
        self.connected = self.reconnect

    async def __call__(self, req):
        if not self.connected:
            raise ConnectionError("Cannot send requests while disconnected")
        self.calls += 1
        if self.drop_after is not None and self.calls > self.drop_after:
            self.connected = False
            raise ConnectionError("Cannot send requests while disconnected")
        return SimpleNamespace(chats=[])


async def test_search_without_connection_does_not_waste_queries(maker, tgbot, monkeypatch):
    import pytest

    monkeypatch.setattr(search.asyncio, "sleep", AsyncMock())
    sent = AsyncMock()
    monkeypatch.setattr(search.notify, "_send", sent)
    with pytest.raises(ConnectionError):
        await search.discover(FakeClient(connected=False), report=True)
    assert "без связи" in sent.call_args.args[0]
    async with maker() as s:
        assert await s.get(Setting, "search_offset") is None  # очередь запросов не сдвинулась
    # связь оборвалась после 3 запросов и не вернулась — следующий проход начнётся с 4-го
    # (на первом этапе вся пачка — про свой регион: его круг откатился к 4-му, общий круг не тронут)
    with pytest.raises(ConnectionError):
        await search.discover(FakeClient(drop_after=3), report=True)
    async with maker() as s:
        assert await s.get(Setting, "search_offset") is None
        assert (await s.get(Setting, search.HOME_KEY)).value == "3"
    assert "прервался" in sent.call_args.args[0]


async def test_search_reconnects_and_continues(maker, tgbot, monkeypatch):
    monkeypatch.setattr(search.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(search.notify, "_send", AsyncMock())
    client = FakeClient(connected=False, reconnect=True)
    assert await search.discover(client) == 0
    assert client.calls == search.PER_PASS


async def test_join_without_connection_does_not_touch_groups(maker):
    from app.scout import joiner

    async with maker() as s:
        s.add(ScoutAccount(id=1, name="sc", api_id=1, api_hash="x", enabled=True, status="ok"))
        s.add(Group(tg_id=-7, title="G", username="g", status="approved", kind="group"))
        await s.commit()
    assert await joiner.join_one(FakeClient(connected=False), 1) is False
    async with maker() as s:
        g = await s.scalar(select(Group).where(Group.tg_id == -7))
        assert g.status == "approved" and g.join_attempts == 0


async def _chats(chat):
    return SimpleNamespace(chats=[chat])


def test_priority_from_home_region():
    """«От Дагестана дальше»: свой регион → соседи → Россия → заграница."""
    from app.services import geo

    home = "Дагестан"
    assert geo.priority("", "", "Барахолка Каспийск", home) == 0
    assert geo.priority("Махачкала", "", "x", home) == 0
    assert geo.priority("", "", "Объявления Грозный", home) == 1
    assert geo.priority("", "", "Барахолка Москва", home) == 2
    assert geo.priority("", "", "Паттайя Барахолка №1 🇹🇭", home) == 3
