"""Проверка оплаченного размещения: отсутствие ответа не подтверждает пост."""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import select

from app.models import AdOrder, AdPost, Group, ScoutAccount, now
from app.scout import publisher
from app.services import adposts
from tests.conftest import make_user
from tests.test_adposts import setup_group, make_order, manager


async def published(maker):
    await setup_group(maker)
    await make_user(maker, 70)
    oid = await make_order(maker)
    async with maker() as s:
        await adposts.approve(s, await s.get(AdOrder, oid))
    mgr, client = manager()
    await publisher.publish_one(mgr)
    async with maker() as s:
        post = await s.scalar(select(AdPost))
        post.sent_at = now() - timedelta(hours=25)
        await s.commit()
    return mgr, client


async def test_network_failure_does_not_complete_verification(maker, tgbot):
    mgr, client = await published(maker)
    async with maker() as s:
        post = await s.scalar(select(AdPost))
        post.checks = 2
        await s.commit()
    client.get_messages = AsyncMock(side_effect=ConnectionError())
    await publisher.check_posts(mgr)
    async with maker() as s:
        post = await s.scalar(select(AdPost))
        assert post.checks == 2 and not post.checked and post.verified_at is None
    client.get_messages = AsyncMock(return_value=SimpleNamespace(id=555))
    await publisher.check_posts(mgr)
    async with maker() as s:
        post = await s.scalar(select(AdPost))
        assert post.checks == 3 and post.checked and post.verified_at is not None


async def test_missing_account_does_not_consume_check(maker, tgbot):
    mgr, _ = await published(maker)
    mgr.clients.clear()
    for _ in range(3):
        await publisher.check_posts(mgr)
    async with maker() as s:
        post = await s.scalar(select(AdPost))
        assert post.checks == 0 and not post.checked and post.verified_at is None


async def test_failing_account_does_not_starve_other_accounts(maker, tgbot):
    mgr, bad = await published(maker)
    async with maker() as s:
        original = await s.scalar(select(AdPost))
        for _ in range(2):
            order = AdOrder(user_id=70, text="Тест проверки размещения", status="done")
            s.add(order)
            await s.flush()
            s.add(AdPost(order_id=order.id, group_id=original.group_id, status="sent", message_id=555,
                         sent_at=now()-timedelta(hours=26)))
        s.add(ScoutAccount(id=2, name="second", api_id=2, api_hash="dummy", enabled=True, can_post=True))
        await s.flush()
        group = Group(tg_id=-200, title="Тестовая площадка", status="joined", account_id=2, ads_allowed=True)
        s.add(group)
        await s.flush()
        order = AdOrder(user_id=70, text="Тест проверки второго аккаунта", status="done")
        s.add(order)
        await s.flush()
        post = AdPost(order_id=order.id, group_id=group.id, status="sent", message_id=556,
                      sent_at=now()-timedelta(hours=1))
        s.add(post)
        await s.commit()
        good_id = post.id
    bad.get_messages = AsyncMock(side_effect=ConnectionError())
    good = AsyncMock()
    good.get_entity.return_value = object()
    good.get_messages.return_value = SimpleNamespace(id=556)
    mgr.clients[2] = good
    await publisher.check_posts(mgr)
    async with maker() as s:
        post = await s.get(AdPost, good_id)
        assert post.checks == 1 and post.verified_at is not None
        others = (await s.scalars(select(AdPost).where(AdPost.id != good_id))).all()
        assert all(p.checks == 0 and not p.checked for p in others)


async def test_ad_inventory_explains_own_prices_and_local_shortage(maker):
    await setup_group(maker, price=25)
    async with maker() as s:
        group = await s.scalar(select(Group))
        group.city, group.region, group.title, group.ad_price = "", "", "Площадка без указанного города", 1
        await s.commit()
        text = "\n".join(await adposts.availability(s))
        assert "Дагестан): 0" in text
        assert "1–1 ₽" in text and "общая цена: 25 ₽" in text
        assert "заменяет общую" in text
