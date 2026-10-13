"""Рассылка администратора и оповещения об ошибках."""
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy import select

from app.bot import notify
from app.bot.handlers import broadcast
from app.models import User, now
from tests.conftest import ADMIN_ID, make_user


async def test_audiences(maker):
    await make_user(maker, 10, plan="full", plan_until=now() + timedelta(days=5))
    await make_user(maker, 11)
    await make_user(maker, 12, blocked=True)
    async with maker() as s:
        s.add(User(tg_id=13, onboarded=False))  # не завершил регистрацию — рассылку не получает
        await s.commit()
    assert set(await broadcast.recipients("all")) == {ADMIN_ID, 10, 11}
    assert await broadcast.recipients("paid") == [10] or set(await broadcast.recipients("paid")) >= {10}
    assert 11 in await broadcast.recipients("free") and 10 not in await broadcast.recipients("free")


async def test_send_all_counts_and_marks_blocked(maker, tgbot):
    await make_user(maker, 20)
    await make_user(maker, 21)
    tgbot.blocked.add(21)
    ok, fail = await broadcast._send_all(tgbot, ADMIN_ID, "all", "Привет! Цены меняются.")
    assert ok == 2 and fail == 1  # админ и 20 получили, 21 заблокировал бота
    assert "Цены меняются" in tgbot.texts(20)[0] and not tgbot.texts(21)
    async with maker() as s:
        assert (await s.get(User, 21)).blocked
    assert any("доставлено 2" in t for t in tgbot.texts(ADMIN_ID))


async def test_preview_then_cancel(maker, tgbot):
    m = MagicMock()
    m.from_user.id = ADMIN_ID
    m.answer = AsyncMock()
    async with maker() as s:
        await broadcast.bc_text(s, m, {"aud": "all"}, "Текст")
    assert "Предпросмотр" in m.answer.call_args.args[0]
    c = MagicMock()
    c.from_user.id = ADMIN_ID
    c.answer, c.message.edit_reply_markup = AsyncMock(), AsyncMock()
    await broadcast.cancel(c)
    assert ADMIN_ID not in broadcast._pending
    c.answer = AsyncMock()
    await broadcast.go(c)
    assert "Нет сообщения" in c.answer.call_args.args[0]  # после отмены отправлять нечего


async def test_alert_once_has_cooldown(maker, tgbot):
    notify._last_alert.clear()
    await notify.alert_once("k", "сломалось")
    await notify.alert_once("k", "сломалось")  # повтор в течение 15 минут — не шлём
    await notify.alert_once("other", "другое")
    texts = tgbot.texts(ADMIN_ID)
    assert sum("сломалось" in t for t in texts) == 1 and any("другое" in t for t in texts)
