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
        b1, start1, total = await search.next_batch(s)
        b2, start2, _ = await search.next_batch(s)
        assert len(b1) == search.PER_PASS and start1 == 0 and start2 == search.PER_PASS and set(b1).isdisjoint(b2)
        assert total == len(all_q)


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

    async def client(req):
        return SimpleNamespace(chats=[chat])

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
