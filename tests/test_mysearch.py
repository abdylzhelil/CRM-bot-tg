"""«🔎 Найти группы для меня»: запросы пользователя, поиск разведчиком, приоритет в раздаче, кнопки."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select

from app.models import Delivery, Group, User, UserSearch
from app.scout import search
from app.services import intake, mysearch
from app.services.antispam import AuthorInfo
from app.services.pipeline import Incoming
from tests.conftest import make_user


async def test_add_validates_and_limits(maker):
    await make_user(maker, 70)
    async with maker() as s:
        u = await s.get(User, 70)
        for w in ("ремонт Подольск", "свадьбы Казань", "торты Москва"):
            await mysearch.add(s, u, w)
        with pytest.raises(mysearch.SearchError, match="до 3"):
            await mysearch.add(s, u, "барахолка Тула")  # бесплатный — 3
        with pytest.raises(mysearch.SearchError, match="уже есть"):
            await mysearch.add(s, u, "Ремонт  подольск")
        with pytest.raises(mysearch.SearchError, match="словами"):
            await mysearch.add(s, u, "12")
        u.plan = "full"
        await mysearch.add(s, u, "барахолка Тула")  # с 💎 — до 10
        await s.commit()
        assert len(await mysearch.my(s, 70)) == 4


async def test_user_pass_finds_groups_for_user(maker, tgbot, monkeypatch):
    from app.scout import client as scout_client
    from app.services import settings

    await make_user(maker, 71)
    async with maker() as s:
        await settings.put(s, "mode", "semi")  # даже без автоотбора группы для пользователя идут в очередь
        s.add(UserSearch(user_id=71, text="ремонт Подольск"))
        s.add(Group(tg_id=-9002, title="Уже известная", status="joined"))
        await s.commit()
    monkeypatch.setattr(scout_client, "ensure_connected", AsyncMock(return_value=True))
    monkeypatch.setattr(search.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(search, "Channel", SimpleNamespace)
    monkeypatch.setattr(search.utils, "get_peer_id", lambda c: c.id)
    monkeypatch.setattr(search, "_sample", AsyncMock(return_value=(50, [])))
    new = SimpleNamespace(megagroup=True, id=-9001, title="Ремонт Подольск чат", username="rem")
    old = SimpleNamespace(megagroup=True, id=-9002, title="Уже известная", username="old")

    async def call(req):  # «клиент Telethon»: поиск и данные группы
        if type(req).__name__ == "SearchRequest":
            return SimpleNamespace(chats=[new, old])
        return SimpleNamespace(full_chat=SimpleNamespace(participants_count=900))
    sent = AsyncMock()
    monkeypatch.setattr(search.notify, "user_search_result", sent)
    assert await search.user_pass(call) == 1
    async with maker() as s:
        g = await s.scalar(select(Group).where(Group.tg_id == -9001))
        q = await s.scalar(select(UserSearch))
        assert await mysearch.has_new(s) is False
    assert g.status == "approved" and g.wanted_by == 71 and g.wanted_search == q.id
    assert q.found == 1 and q.last_run_at is not None
    assert sent.call_args.args[0] == 71 and sent.call_args.args[2]["found"] == 1 and sent.call_args.args[2]["known"] == 1
    # повторно сразу не ищет — только через user_search_days
    async with maker() as s:
        assert await mysearch.due(s) == []
    # удаление запроса снимает приоритет с группы
    async with maker() as s:
        assert await mysearch.remove(s, 71, q.id)
        await s.commit()
        assert (await s.scalar(select(Group).where(Group.tg_id == -9001))).wanted_by == 0


async def test_wanted_user_gets_leads_first(maker, tgbot):
    await make_user(maker, 72)          # бесплатный, нашёл группу
    await make_user(maker, 73, plan="full")
    async with maker() as s:
        s.add(Group(tg_id=-9100, title="Ремонт чат", status="joined", wanted_by=72, wanted_search=1))
        await s.commit()
    lead = await intake.handle(Incoming(text="Ищу кто сделает лендинг для магазина срочно", author=AuthorInfo(
        user_id=99001, username="cl", first_name="Али"), group_tg_id=-9100, group_title="Ремонт чат", message_id=1))
    async with maker() as s:
        d = {x.user_id: x for x in (await s.scalars(select(Delivery).where(Delivery.lead_id == lead.id))).all()}
    assert 72 in d and 73 in d


async def test_buttons(maker, tgbot):
    from app.bot.handlers import catalog as ch
    from app.bot.handlers import inputs
    from app.bot.handlers import mysearch as mh

    user = await make_user(maker, 74)
    m = MagicMock()
    m.answer, m.from_user.id = AsyncMock(), 74
    async with maker() as s:
        assert await inputs.HANDLERS["my_search"](s, m, {}, "ремонт Подольск") is None
    assert "добавлен" in m.answer.call_args.args[0]
    c = MagicMock()
    c.answer, c.message.answer, c.message.edit_text = AsyncMock(), AsyncMock(), AsyncMock()
    c.data = "ms:list"
    await mh.ms_list(c, user)
    text = c.answer.call_args.args[0]  # MagicMock — как сообщение: reply() отвечает новым сообщением
    assert "ремонт Подольск" in text and "ещё не искали" in text
    # кнопка есть в каталоге
    c2 = MagicMock()
    c2.answer, c2.message.answer, c2.message.edit_text = AsyncMock(), AsyncMock(), AsyncMock()
    await ch.catalog_menu(c2, user)
    markup = c2.answer.call_args.kwargs.get("reply_markup")
    assert any(b.callback_data == "ms:list" for row in markup.inline_keyboard for b in row)
