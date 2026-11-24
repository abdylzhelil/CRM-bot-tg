"""Восстановление оплаченной покупки после задержки опроса провайдера."""
from datetime import timedelta

from sqlalchemy import select

from app import config
from app.bot.handlers import payments as handler
from app.models import AdOrder, AdPost, Payment, PendingLink, User, now
from app.services import adposts, payments
from tests.conftest import make_user
from tests.test_adposts import setup_group, make_order
from tests.test_payments import FakeYK, set_price


async def test_paid_subscription_is_not_lost_after_three_hours(maker, tgbot, monkeypatch):
    monkeypatch.setattr(config, "YOOKASSA_SHOP_ID", "1")
    monkeypatch.setattr(config, "YOOKASSA_SECRET_KEY", "dummy")
    await set_price(maker, 30, 490)
    await make_user(maker, 801)
    api = FakeYK()
    async with maker() as s:
        await payments.create_link(s, 801, 30, "https://t.me/example", api=api)
        (await s.scalar(select(PendingLink))).created_at = now() - timedelta(hours=5)
        await s.commit()
    api.payments["yk-1"].update(status="succeeded", paid=True)
    assert await handler.process_links(api) == 1
    assert await handler.process_links(api) == 0
    async with maker() as s:
        assert (await s.get(User, 801)).plan == "full"
        assert await s.scalar(select(Payment.amount)) == 49000


async def test_paid_ad_is_not_lost_after_polling_delay(maker, tgbot, monkeypatch):
    monkeypatch.setattr(config, "YOOKASSA_SHOP_ID", "1")
    monkeypatch.setattr(config, "YOOKASSA_SECRET_KEY", "dummy")
    await setup_group(maker, price=300)
    await make_user(maker, 70)
    oid = await make_order(maker)
    api = FakeYK()
    async with maker() as s:
        order = await s.get(AdOrder, oid)
        await adposts.approve(s, order)
        await payments.create_ad_link(s, order, "https://t.me/example", api=api)
        (await s.scalar(select(PendingLink))).created_at = now() - timedelta(hours=5)
        await s.commit()
    api.payments["yk-1"].update(status="succeeded", paid=True)
    assert await handler.process_links(api) == 1
    assert await handler.process_links(api) == 0
    async with maker() as s:
        assert (await s.get(AdOrder, oid)).status == "running"
        assert len((await s.scalars(select(AdPost))).all()) == 1
        assert (await s.scalar(select(Payment))).amount == 30000
