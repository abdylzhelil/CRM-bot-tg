"""Реклама для админов групп: заявки группы -> «Рекомендуем» в каталоге и канале статистики."""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select

from app import config
from app.bot.handlers import catalog as ch
from app.bot.handlers import submissions
from app.models import Group, User, now
from app.services import ads, catalog, public_stats
from tests.conftest import ADMIN_ID, make_user


async def add_group(maker, owner=50, leads=25, tg_id=-700, title="Моя группа", **kw):
    async with maker() as s:
        s.add(Group(tg_id=tg_id, username=f"g{abs(tg_id)}", title=title, kind="group", status="joined", catalog=True,
                    submitted_by=owner, leads_count=leads, **kw))
        await s.commit()
        return (await s.scalar(select(Group).where(Group.tg_id == tg_id))).id


def cbq(data, uid):
    c = MagicMock()
    c.data = data
    c.from_user.id = uid
    c.answer, c.message.edit_reply_markup, c.message.answer = AsyncMock(), AsyncMock(), AsyncMock()
    c.bot.send_message = AsyncMock()
    return c


async def test_ad_order_approve_and_catalog_order(maker, tgbot):
    await make_user(maker, 50)
    gid = await add_group(maker, 50, leads=25)
    await add_group(maker, 51, leads=100, tg_id=-701, title="Популярная")  # чужая, без рекламы
    owner = SimpleNamespace(tg_id=50, name="Али", username="", is_admin=False)
    c = cbq(f"ad:go:{gid}", 50)
    await ch.ad_go(c, owner)
    assert any("Заказ рекламы" in t for t in tgbot.texts(ADMIN_ID))
    again = cbq(f"ad:go:{gid}", 50)
    await ch.ad_go(again, owner)
    assert "уже отправлена" in again.answer.call_args.args[0]  # второй заказ, пока первый не обработан, нельзя

    ok = cbq(f"adm_ad:ok:{gid}", ADMIN_ID)
    await submissions.ad_decide(ok)
    async with maker() as s:
        g = await s.get(Group, gid)
        assert g.ad_spent == 20 and not g.ad_pending and timedelta(days=6) < g.ad_until - now() < timedelta(days=8)
        user = User(tg_id=77)
        listing = await catalog.browse(s, user, set(), everything=True)
    assert listing[0].id == gid  # рекламируемая выше более «популярной»
    ok.bot.send_message.assert_awaited()


async def test_ad_requires_enough_leads_and_ownership(maker, tgbot):
    gid = await add_group(maker, 50, leads=19)
    async with maker() as s:
        with pytest.raises(ads.AdError, match="доступно 19"):
            await ads.request(s, 50, gid)
        with pytest.raises(ads.AdError, match="только для своей"):
            await ads.request(s, 99, gid)


async def test_ad_reject_keeps_leads_and_extend(maker, tgbot):
    gid = await add_group(maker, 50, leads=60, ad_until=now() + timedelta(days=3), ad_spent=0)
    async with maker() as s:
        g = await ads.request(s, 50, gid)
        await ads.reject(s, g)
        assert g.ad_spent == 0 and not g.ad_pending  # отклонили — заявки целы
        g = await ads.request(s, 50, gid)
        await ads.approve(s, g)
        assert timedelta(days=9) < g.ad_until - now() < timedelta(days=11)  # продлевает действующую рекламу


async def test_featured_in_public_stats_escaped(maker, tgbot):
    await add_group(maker, 50, title="Чат <b>&", ad_until=now() + timedelta(days=2))
    await add_group(maker, 51, tg_id=-702, title="Без рекламы")
    text = await public_stats.build()
    assert "📢 Рекомендуем" in text and "Чат &lt;b&gt;&amp;" in text and "Без рекламы" not in text
