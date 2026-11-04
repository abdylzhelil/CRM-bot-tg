"""Автопродление по сохранённой карте и лицензия за заявки группы."""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy import select

from app import config
from app.bot.handlers import licenses as lic_h
from app.bot.handlers import payments as ph
from app.feed.server import license_status
from app.main import expiry_once
from app.models import Group, Payment, User, now
from app.services import intake, licenses, payments, settings
from app.services.antispam import AuthorInfo
from app.services.pipeline import Incoming
from tests.conftest import ADMIN_ID, make_user
from tests.test_payments import FakeYK


async def keys(monkeypatch):
    monkeypatch.setattr(config, "YOOKASSA_SHOP_ID", "1")
    monkeypatch.setattr(config, "YOOKASSA_SECRET_KEY", "k")


async def test_link_with_autorenew_saves_card_and_renews(maker, tgbot, monkeypatch):
    await keys(monkeypatch)
    async with maker() as s:
        await settings.put(s, "price_30", "490")
        await settings.put(s, "autorenew", "1")
    await make_user(maker, 70)
    yk = FakeYK()
    async with maker() as s:
        await payments.create_link(s, 70, 30, "https://t.me/x", api=yk, auto=True)
    assert yk.calls[0][2]["save_payment_method"] is True  # карта запоминается только по согласию
    yk.payments["yk-1"].update(status="succeeded", paid=True, payment_method={"id": "pm-1", "saved": True, "card": {"last4": "1234", "card_type": "MasterCard"}})
    assert await ph.process_links(yk) == 1
    async with maker() as s:
        u = await s.get(User, 70)
        assert u.renew_method == "pm-1" and u.renew_days == 30 and u.renew_card == "Mastercard •••• 1234"  # номер целиком не храним
        u.plan_until = now() + timedelta(hours=5)  # тариф кончается меньше чем через сутки
        await s.commit()
    # продление: ЮKassa списывает с сохранённой карты
    yk.calls.clear()
    orig = yk.__call__

    async def charge(method, path, body=None, key=""):
        if method == "POST" and body.get("payment_method_id"):
            yk.calls.append((method, path, body))
            return {"id": "yk-renew", "status": "succeeded", "_http": 200}
        return await orig(method, path, body, key)

    told = []

    async def tell(uid, text, good):
        told.append((uid, good))

    async with maker() as s:
        assert await payments.renew_due(s, api=charge, notify_user=tell) == 1
        assert await payments.renew_due(s, api=charge, notify_user=tell) == 0  # одна попытка на срок
    assert yk.calls[0][2]["payment_method_id"] == "pm-1" and yk.calls[0][2]["amount"]["value"] == "490.00"
    async with maker() as s:
        u = await s.get(User, 70)
        pays = (await s.scalars(select(Payment).where(Payment.user_id == 70))).all()
    assert len(pays) == 2 and u.plan_until - now() > timedelta(days=29)
    assert told == [(70, True)]


async def test_renew_failure_disables_autorenew(maker, tgbot, monkeypatch):
    await keys(monkeypatch)
    async with maker() as s:
        await settings.put(s, "price_30", "490")
        await settings.put(s, "autorenew", "1")
    await make_user(maker, 71, plan="full", plan_until=now() + timedelta(hours=3), renew_method="pm-x", renew_days=30)

    async def declined(method, path, body=None, key=""):
        return {"status": "canceled", "_http": 200}

    told = []

    async def tell(uid, text, good):
        told.append((uid, good, text))

    async with maker() as s:
        assert await payments.renew_due(s, api=declined, notify_user=tell) == 0
    async with maker() as s:
        u = await s.get(User, 71)
    assert u.renew_method == "" and told[0][1] is False and "отключено" in told[0][2]


async def test_autorenew_off_flag_and_user_button(maker, tgbot, monkeypatch):
    await keys(monkeypatch)
    await make_user(maker, 72, plan="full", plan_until=now() + timedelta(hours=3), renew_method="pm", renew_days=30)
    async with maker() as s:
        assert await payments.renew_due(s, api=AsyncMock()) == 0  # настройка autorenew выключена — ничего не списываем
    c = MagicMock()
    c.answer = AsyncMock()
    await ph.renew_off(c, SimpleNamespace(tg_id=72))
    async with maker() as s:
        assert (await s.get(User, 72)).renew_method == ""


async def test_reminder_mentions_charge_for_autorenew(maker, tgbot, monkeypatch):
    await keys(monkeypatch)
    async with maker() as s:
        await settings.put(s, "price_30", "490")
        await settings.put(s, "autorenew", "1")
    await make_user(maker, 73, plan="full", plan_until=now() + timedelta(days=2), renew_method="pm", renew_days=30)
    await expiry_once()
    assert any("спишем 490" in t for t in tgbot.texts(73))


async def test_autorenew_switched_off_plan_still_ends(maker, tgbot, monkeypatch):
    """Карта привязана, но владелец выключил автопродление: не обещаем списание и тариф честно заканчивается."""
    await keys(monkeypatch)
    async with maker() as s:
        await settings.put(s, "price_30", "490")
    await make_user(maker, 74, plan="full", plan_until=now() + timedelta(days=2), renew_method="pm", renew_days=30)
    await make_user(maker, 75, plan="full", plan_until=now() - timedelta(hours=1), renew_method="pm", renew_days=30)
    await expiry_once()
    assert not any("спишем" in t for t in tgbot.texts(74))
    async with maker() as s:
        assert (await s.get(User, 75)).plan == "free"


async def test_renew_pending_is_watched_not_dropped(maker, tgbot, monkeypatch):
    """ЮKassa ответила «pending»: автопродление не выключаем, платёж ставим под наблюдение."""
    from app.models import PendingLink

    await keys(monkeypatch)
    async with maker() as s:
        await settings.put(s, "price_30", "490")
        await settings.put(s, "autorenew", "1")
    await make_user(maker, 76, plan="full", plan_until=now() + timedelta(hours=3), renew_method="pm", renew_days=30)
    async with maker() as s:
        assert await payments.renew_due(s, api=AsyncMock(return_value={"id": "p-1", "status": "pending"})) == 0
    async with maker() as s:
        assert (await s.get(User, 76)).renew_method == "pm"
        link = await s.scalar(select(PendingLink))
        assert link.yk_id == "p-1" and link.days == 30 and link.amount == 49000


async def add_group(maker, leads=0, **kw):
    async with maker() as s:
        s.add(Group(tg_id=-900, username="lg", title="Группа с лицензией", kind="group", status="joined", catalog=True,
                    submitted_by=50, leads_count=leads, **kw))
        await s.commit()
        return (await s.scalar(select(Group))).id


async def test_license_awarded_by_leads_and_manual_off(maker, tgbot):
    await make_user(maker, 50)
    gid = await add_group(maker, leads=49)
    async with maker() as s:
        g = await s.get(Group, gid)
        assert not await licenses.award(s, g)  # выключено в настройках
        await settings.put(s, "license_enabled", "1")
        assert not await licenses.award(s, g)  # 49 < 50
        g.leads_count = 120
        assert await licenses.award(s, g)  # 2 периода по 50 = 60 дней
        assert timedelta(days=59) < g.license_until - now() < timedelta(days=61) and licenses.active(g)
        await licenses.toggle(s, g)  # владелец отключил вручную
        assert not licenses.active(g)
        g.leads_count = 400
        assert not await licenses.award(s, g)  # пока отключено, новая не выдаётся
        await licenses.toggle(s, g)
        await licenses.grant(s, g, 30)
        assert timedelta(days=89) < g.license_until - now() < timedelta(days=91)


async def test_license_http_answer_and_admin_buttons(maker, tgbot, monkeypatch):
    await make_user(maker, 50)
    gid = await add_group(maker, license_until=now() + timedelta(days=5))
    req = MagicMock()
    req.query = {"chat_id": "-900"}
    resp = await license_status(req)
    assert b'"active": true' in resp.body
    req.query = {"chat_id": "-1"}
    assert b'"active": false' in (await license_status(req)).body
    c = MagicMock()
    c.answer, c.message.edit_text, c.message.answer = AsyncMock(), AsyncMock(), AsyncMock()
    c.data = f"lic:tog:{gid}"
    await lic_h.act(c)
    async with maker() as s:
        assert (await s.get(Group, gid)).license_off
    req.query = {"chat_id": "-900"}
    assert b'"active": false' in (await license_status(req)).body  # отключённая вручную не считается действующей


# ---------- привязанная карта: показ и отвязка (требование ЮKassa к интерфейсу) ----------
def test_card_label_shows_type_and_last4_only():
    assert payments.card_label({"card": {"last4": "4242", "card_type": "Mir"}}) == "Mir •••• 4242"
    assert payments.card_label({"card": {"last4": "9999", "card_type": "MasterCard", "first6": "555555"}}) == "Mastercard •••• 9999"
    assert payments.card_label({"card": {"last4": "1111"}}) == "Карта •••• 1111"
    assert payments.card_label({"title": "Bank card *1234"}) == "Bank card *1234"
    assert payments.card_label({}) == "Банковская карта"
