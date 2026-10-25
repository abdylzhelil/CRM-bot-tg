"""🔕 Выключить уведомления о заявках на время: заявки копятся без сообщений, напоминание, включение по сроку."""
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy import select

from app.bot import notify
from app.models import Delivery, User, now
from app.services import intake, mute
from app.services.antispam import AuthorInfo
from app.services.pipeline import Incoming
from tests.conftest import make_user


def msg(n):
    return Incoming(text=f"Ищу кто сделает лендинг для магазина номер {n}", author=AuthorInfo(
        user_id=88000 + n, username="cl", first_name="Али"), group_tg_id=-100777, group_title="Чат", message_id=n)


def cb(data):
    c = MagicMock()
    c.data = data
    c.answer, c.message.answer, c.message.edit_text = AsyncMock(), AsyncMock(), AsyncMock()
    return c


async def test_muted_user_gets_leads_into_list_without_messages(maker, tgbot):
    from app.bot.handlers import leads as leads_h
    from app.bot.handlers import mute as mute_h

    user = await make_user(maker, 60, plan="full")
    await mute_h.mt_set(cb("mt:set:3h"), user)
    async with maker() as s:
        row = await s.get(User, 60)
        assert mute.is_muted(row) and mute.until_text(row).startswith("до ")
    for n in range(2):
        await intake.handle(msg(n))
    await notify.flush_due()
    assert not tgbot.texts(60)  # сообщений с заявками не было
    async with maker() as s:
        ds = (await s.scalars(select(Delivery).where(Delivery.user_id == 60))).all()
        assert len(ds) == 2 and all(d.sent and d.muted for d in ds)
        assert await mute.accumulated(s, 60) == 2
    # список «накопилось» открывается
    c = cb("ll:muted:0")
    await leads_h.leads_list(c, user)
    shown = [c_.args[0] for c_ in c.answer.call_args_list + c.message.answer.call_args_list if c_.args]
    assert any("Накопилось" in x and "из 2" in x for x in shown)  # заявки по одной: «1 из 2»
    # включил сам — итог «накопилось 2»
    c = cb("mt:on")
    await mute_h.mt_on(c, user)
    text = (c.message.edit_text.call_args or c.message.answer.call_args or c.answer.call_args).args[0]
    assert "накопилось заявок: 2" in text
    async with maker() as s:
        assert not mute.is_muted(await s.get(User, 60))


async def test_reminder_on_entering_and_auto_expiry(maker, tgbot, monkeypatch):
    from app.bot.handlers import mute as mute_h

    user = await make_user(maker, 61)
    async with maker() as s:
        row = await s.get(User, 61)
        row.mute_until, row.mute_noted_at = now() + timedelta(hours=5), None
        await s.commit()
    m = MagicMock()
    m.answer = AsyncMock()
    await mute_h.remind(m, user)
    assert "выключены" in m.answer.call_args.args[0]
    m.answer.reset_mock()
    await mute_h.remind(m, user)  # второй раз в течение часа — не напоминаем
    m.answer.assert_not_called()
    # срок вышел — включаются сами
    async with maker() as s:
        row = await s.get(User, 61)
        row.mute_until = now() - timedelta(minutes=1)
        await s.commit()
        done = await mute.expired(s)
        await s.commit()
        assert [u.tg_id for u in done] == [61] and (await s.get(User, 61)).mute_until is None


def test_options():
    assert mute.until_for("1h") - now() <= timedelta(hours=1, seconds=1)
    assert timedelta(0) < mute.until_for("morning") - now() <= timedelta(days=1)
    assert mute.until_for("off") - now() > timedelta(days=365)
