"""«🧪 Самопроверка»: находит проблемы, ничего не меняя."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from app.models import ScoutAccount
from app.services import offers, selfcheck


def test_filter_samples_hold():
    for text, want in selfcheck.SAMPLES:
        assert offers.rule_verdict(text) == want, text


async def test_selfcheck_finds_problems(maker, monkeypatch):
    async with maker() as s:
        s.add(ScoutAccount(id=1, name="sc", api_id=1, api_hash="x", enabled=True, status="ok"))
        await s.commit()

    async def boom():
        raise RuntimeError("упало")

    task = asyncio.create_task(boom())
    await asyncio.sleep(0)
    monkeypatch.setattr(selfcheck, "TASKS", {"раздача заявок": task})
    bot = SimpleNamespace(get_me=AsyncMock(return_value=SimpleNamespace(username="klientradarbot")))
    async with maker() as s:
        rows = await selfcheck.run(s, bot, SimpleNamespace(clients={}))
    text = selfcheck.render(rows)
    assert "@klientradarbot на связи" in text
    assert "⛔ <b>Разведчик «sc»</b>: не подключён" in text
    assert "остановились: раздача заявок (RuntimeError)" in text
    assert "верно на всех" in text


async def test_selfcheck_button(maker, tgbot):
    from app.bot.handlers import settings as st

    c = MagicMock()
    c.answer, c.message.answer = AsyncMock(), AsyncMock()
    c.bot.get_me = AsyncMock(return_value=SimpleNamespace(username="b"))
    await st.self_check(c)
    assert "Самопроверка" in c.message.answer.call_args_list[0].args[0]
