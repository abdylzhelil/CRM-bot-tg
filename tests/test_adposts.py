"""Реклама в группах: заказ → проверка админом → (оплата) → публикация разведчиком с лимитами."""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import func, select
from telethon.errors import ChatWriteForbiddenError, FloodWaitError, PeerFloodError

from app import config
from app.bot.handlers import adposts as ah
from app.bot.handlers import payments as ph
from app.models import AdOrder, AdPost, Group, ScoutAccount, User, now
from app.scout import publisher
from app.services import adposts, payments, settings
from tests.conftest import ADMIN_ID, make_user

TEXT = "Ремонт квартир под ключ в Махачкале, звоните: 8-900-000-00-00"


async def setup_group(maker, allowed=True, on=True, price=0, gid=-100):
    async with maker() as s:
        s.add(ScoutAccount(id=1, name="sc", api_id=1, api_hash="x", enabled=True, can_post=True, status="ok"))
        s.add(Group(tg_id=gid, username="barakholka", title="Барахолка", members=5000, status="joined", kind="group",
                    account_id=1, ads_allowed=allowed, city="Махачкала"))
        await settings.put(s, "adpost_enabled", "1" if on else "0")
        await settings.put(s, "adpost_price", str(price))
        await s.commit()


def manager(send=None, acc=1):
    client = AsyncMock()
    client.get_entity = AsyncMock(return_value=object())
    client.send_message = send or AsyncMock(return_value=SimpleNamespace(id=555))
    return SimpleNamespace(clients={acc: client}), client


async def make_order(maker, user_id=70, price_per=0):
    async with maker() as s:
        order = await adposts.new_draft(s, user_id, TEXT)
        gid = (await s.scalar(select(Group))).id
        await adposts.toggle_group(s, order, gid)
        await adposts.submit(s, order)
        return order.id


async def test_free_order_published_and_user_gets_links(maker, tgbot):
    await setup_group(maker)
    await make_user(maker, 70)
    oid = await make_order(maker)
    async with maker() as s:
        order = await s.get(AdOrder, oid)
        assert order.status == "pending" and order.price == 0
        assert await adposts.approve(s, order) == "running"
    mgr, client = manager()
    assert await publisher.publish_one(mgr) is True
    sent_text = client.send_message.await_args.args[1]
    assert client.send_message.await_args.kwargs["parse_mode"] is None  # текст как есть, без Markdown
    assert sent_text.startswith(TEXT) and sent_text.endswith("Реклама")  # подпись-пометка под постом
    async with maker() as s:
        post = await s.scalar(select(AdPost))
        order = await s.get(AdOrder, oid)
    assert post.status == "sent" and post.link == "https://t.me/barakholka/555" and order.status == "done"
    assert any("выполнен" in t and "barakholka/555" in t for t in tgbot.texts(70))


async def test_paid_order_waits_for_payment(maker, tgbot, monkeypatch):
    monkeypatch.setattr(config, "YOOKASSA_SHOP_ID", "1")
    monkeypatch.setattr(config, "YOOKASSA_SECRET_KEY", "k")
    from tests.test_payments import FakeYK

    await setup_group(maker, price=300)
    await make_user(maker, 70)
    oid = await make_order(maker)
    async with maker() as s:
        order = await s.get(AdOrder, oid)
        assert order.price == 300  # цена зафиксирована на сервере
        assert await adposts.approve(s, order) == "approved"
    mgr, client = manager()
    assert await publisher.publish_one(mgr) is False  # пока не оплачено — не публикуем
    yk = FakeYK()
    async with maker() as s:
        url = await payments.create_ad_link(s, await s.get(AdOrder, oid), "https://t.me/b", api=yk)
    assert url.startswith("https://yookassa.ru/pay/") and yk.calls[0][2]["amount"]["value"] == "300.00"
    assert await ph.process_links(yk) == 0
    yk.payments["yk-1"].update(status="succeeded", paid=True)
    assert await ph.process_links(yk) == 1
    assert await ph.process_links(yk) == 0  # повторно не засчитывается
    async with maker() as s:
        assert (await s.get(AdOrder, oid)).status == "running"
        assert (await s.get(User, 70)).plan == "free"  # тариф оплата рекламы не продлевает
    assert any("Оплата получена" in t for t in tgbot.texts(70)) and any("Оплачена реклама" in t for t in tgbot.texts(ADMIN_ID))
    assert await publisher.publish_one(mgr) is True


async def test_daily_limit_and_gap(maker, tgbot):
    await setup_group(maker)
    await make_user(maker, 70)
    async with maker() as s:
        await settings.put(s, "adpost_daily_limit", "5")
        await settings.put(s, "adpost_gap_min", "10")
        s.add(Group(tg_id=-101, username="g2", title="Вторая", members=100, status="joined", kind="group", account_id=1, ads_allowed=True))
        await s.commit()
        order = await adposts.new_draft(s, 70, TEXT)
        for g in (await s.scalars(select(Group))).all():
            await adposts.toggle_group(s, order, g.id)
        await adposts.submit(s, order)
        await adposts.approve(s, order)
    mgr, client = manager()
    assert await publisher.publish_one(mgr) is True
    assert await publisher.publish_one(mgr) is False  # пауза между постами аккаунта ещё не прошла
    async with maker() as s:
        last = await s.scalar(select(AdPost).where(AdPost.status == "sent"))
        last.sent_at = now() - timedelta(minutes=11)
        await s.commit()
    assert await publisher.publish_one(mgr) is True
    async with maker() as s:
        await settings.put(s, "adpost_daily_limit", "2")
        assert (await s.scalar(select(AdOrder))).status == "done"
    # суточный лимит: 2 уже отправлено сегодня
    async with maker() as s:
        o2 = await adposts.new_draft(s, 70, TEXT + " ещё")
        await adposts.toggle_group(s, o2, (await s.scalar(select(Group))).id)
        await adposts.submit(s, o2)
        await adposts.approve(s, o2)
        for p in (await s.scalars(select(AdPost).where(AdPost.status == "sent"))).all():
            p.sent_at = now() - timedelta(minutes=30)
        await s.commit()
    assert await publisher.publish_one(mgr) is False


async def test_forbidden_group_marks_failed_and_can_retry(maker, tgbot):
    await setup_group(maker)
    await make_user(maker, 70)
    oid = await make_order(maker)
    async with maker() as s:
        await adposts.approve(s, await s.get(AdOrder, oid))
    mgr, client = manager(send=AsyncMock(side_effect=ChatWriteForbiddenError(request=None)))
    assert await publisher.publish_one(mgr) is True
    async with maker() as s:
        post = await s.scalar(select(AdPost))
        # группа закрыта сейчас (ночной режим и т. п.) — не сдаёмся сразу, пробуем через 1–2 часа
        assert post.status == "queued" and post.scheduled_at > now() + timedelta(minutes=50) and "позже" in post.error
        post.first_try_at = now() - timedelta(hours=25)  # прошли сутки, а группа всё закрыта
        post.scheduled_at = now() - timedelta(seconds=1)
        await s.commit()
    assert await publisher.publish_one(mgr) is True
    async with maker() as s:
        post = await s.scalar(select(AdPost))
        order = await s.get(AdOrder, oid)
        assert post.status == "failed" and "нельзя писать" in post.error and order.status == "done"
        assert await adposts.retry_failed(s, order) == 1
        assert order.status == "running" and (await s.scalar(select(AdPost))).status == "queued"
    assert any("не вышло" in t for t in tgbot.texts(ADMIN_ID)) and any("выполнен" in t for t in tgbot.texts(70))


async def test_floodwait_reschedules_and_peerflood_limits_account(maker, tgbot):
    await setup_group(maker)
    await make_user(maker, 70)
    oid = await make_order(maker)
    async with maker() as s:
        await adposts.approve(s, await s.get(AdOrder, oid))
    mgr, _ = manager(send=AsyncMock(side_effect=FloodWaitError(request=None, capture=120)))
    assert await publisher.publish_one(mgr) is True
    async with maker() as s:
        post = await s.scalar(select(AdPost))
        assert post.status == "queued" and post.scheduled_at > now() + timedelta(seconds=100)
        post.scheduled_at = now() - timedelta(seconds=1)
        await s.commit()
    mgr2, _ = manager(send=AsyncMock(side_effect=PeerFloodError(request=None)))
    await publisher.publish_one(mgr2)
    async with maker() as s:
        assert (await s.get(ScoutAccount, 1)).status == "limited"
        assert (await s.scalar(select(AdPost))).scheduled_at > now() + timedelta(hours=20)  # отложено на сутки
    assert any("PeerFlood" in t for t in tgbot.texts(ADMIN_ID))


async def test_disabled_service_and_closed_group(maker, tgbot):
    await setup_group(maker, on=False)
    await make_user(maker, 70)
    async with maker() as s:
        with pytest.raises(adposts.AdError):
            await adposts.new_draft(s, 70, TEXT)  # сервис выключен
        await settings.put(s, "adpost_enabled", "1")
    oid = await make_order(maker)
    async with maker() as s:
        await adposts.approve(s, await s.get(AdOrder, oid))
        g = await s.scalar(select(Group))
        g.ads_allowed = False  # админ закрыл группу после заказа
        await s.commit()
    mgr, client = manager()
    await publisher.publish_one(mgr)
    client.send_message.assert_not_called()
    async with maker() as s:
        assert (await s.scalar(select(AdPost))).status == "failed"


async def test_validation_and_limits(maker):
    await setup_group(maker)
    await make_user(maker, 70)
    async with maker() as s:
        with pytest.raises(adposts.AdError):
            await adposts.new_draft(s, 70, "коротко")
        for i in range(5):  # брошенные черновики не копятся и не блокируют новые заказы
            await adposts.new_draft(s, 70, TEXT + str(i))
        assert await s.scalar(select(func.count()).select_from(AdOrder)) == 1
    for _ in range(3):
        await make_order(maker)
    async with maker() as s:
        with pytest.raises(adposts.AdError, match="3 незавершённых"):
            await adposts.new_draft(s, 70, TEXT)
        order = AdOrder(user_id=70, text=TEXT)
        s.add(order)
        await s.commit()
        with pytest.raises(adposts.AdError, match="хотя бы одну"):
            await adposts.submit(s, order)
        order.group_ids = "999"
        with pytest.raises(adposts.AdError, match="недоступны"):
            await adposts.submit(s, order)  # чужая/закрытая группа не пройдёт


async def test_cancel_and_reject(maker, tgbot):
    await setup_group(maker)
    await make_user(maker, 70)
    oid = await make_order(maker)
    async with maker() as s:
        order = await s.get(AdOrder, oid)
        await adposts.reject(s, order)
        assert order.status == "rejected"
        with pytest.raises(adposts.AdError):
            await adposts.approve(s, order)
    oid2 = await make_order(maker)
    async with maker() as s:
        o2 = await s.get(AdOrder, oid2)
        await adposts.approve(s, o2)
        assert await adposts.cancel(s, o2) == 1 and o2.status == "canceled"
    mgr, client = manager()
    assert await publisher.publish_one(mgr) is False and client.send_message.call_count == 0


def cbq(data, uid=70):
    c = MagicMock()
    c.data = data
    c.answer, c.message.edit_text, c.message.answer = AsyncMock(), AsyncMock(), AsyncMock()
    c.from_user.id = uid
    return c


async def test_screens_user_and_admin(maker, tgbot, monkeypatch):
    sent = AsyncMock()
    monkeypatch.setattr(ah, "reply", sent)
    await setup_group(maker, price=100)
    u = await make_user(maker, 70, city="Махачкала")
    await ah.ap_menu(cbq("ap:menu"), u)
    menu_markup = sent.await_args.args[2]
    assert "ap:new" in str(menu_markup) and "ap:mine" in str(menu_markup)
    async with maker() as s:
        order = await adposts.new_draft(s, 70, TEXT)
        gid = (await s.scalar(select(Group))).id
    await ah.ap_toggle(cbq(f"ap:t:{order.id}:{gid}"), u)
    assert "✅" in str(sent.await_args.args[2])  # группа отмечена
    await ah.ap_review(cbq(f"ap:review:{order.id}"), u)
    assert "Итого: 100 ₽" in sent.await_args.args[1]
    await ah.ap_send(cbq(f"ap:send:{order.id}"), u)
    assert any("Новый заказ рекламы" in t and f"№{order.id}" in t for t in tgbot.texts(ADMIN_ID))
    # чужой заказ открыть нельзя
    other = cbq(f"ap:o:{order.id}", uid=71)
    stranger = await make_user(maker, 71)
    await ah.ap_open(other, stranger)
    assert other.answer.await_args.kwargs.get("show_alert") is True
    # админ одобряет → заказчик получает просьбу оплатить
    await ah.adm_act(cbq(f"apa:ok:{order.id}", uid=ADMIN_ID))
    assert any("Осталось оплатить 100" in t for t in tgbot.texts(70))
    await ah.adm_list(cbq("apa:list", uid=ADMIN_ID))
    assert f"apa:o:{order.id}" in str(sent.await_args.args[2])
    await ah.groups_for_ads(cbq("apg:list", uid=ADMIN_ID))
    assert f"apg:o:{gid}" in str(sent.await_args.args[2])
    await ah.group_card(cbq(f"apg:o:{gid}", uid=ADMIN_ID))
    assert f"apg:tog:{gid}" in str(sent.await_args.args[2]) and f"apg:price:{gid}" in str(sent.await_args.args[2])
    await ah.groups_toggle(cbq(f"apg:tog:{gid}", uid=ADMIN_ID))
    async with maker() as s:
        assert (await s.get(Group, gid)).ads_allowed is False


async def paid_order(maker, tgbot, monkeypatch, price=300):
    from tests.test_payments import FakeYK

    monkeypatch.setattr(config, "YOOKASSA_SHOP_ID", "1")
    monkeypatch.setattr(config, "YOOKASSA_SECRET_KEY", "k")
    await setup_group(maker, price=price)
    await make_user(maker, 70, plan="full", plan_until=now() + timedelta(days=20), renew_method="pm", renew_card="Mir •••• 1234", renew_days=30)
    oid = await make_order(maker)
    async with maker() as s:
        await adposts.approve(s, await s.get(AdOrder, oid))
    yk = FakeYK()
    async with maker() as s:
        await payments.create_ad_link(s, await s.get(AdOrder, oid), "https://t.me/b", api=yk)
    yk.payments["yk-1"].update(status="succeeded", paid=True)
    assert await ph.process_links(yk) == 1
    return oid


async def test_ad_payment_is_recorded_for_revenue_receipts_and_refund(maker, tgbot, monkeypatch):
    from app.models import Payment

    oid = await paid_order(maker, tgbot, monkeypatch)
    async with maker() as s:
        pay = await s.scalar(select(Payment))
        assert pay.days == 0 and pay.amount == 30000 and pay.provider_id == "yk-1" and not pay.refunded  # попадёт в выручку и чеки
    # админу пришла кнопка возврата, возврат отменяет посты и НЕ трогает тариф и привязанную карту покупателя
    async with maker() as s:
        pay = await s.scalar(select(Payment))
        yk_call = AsyncMock(return_value={"status": "succeeded"})
        note = await payments.refund(s, pay, True, call=yk_call)
        order = await s.get(AdOrder, oid)
        user = await s.get(User, 70)
        assert "возвращены" in note and order.status == "canceled" and pay.refunded
        assert user.plan == "full" and user.renew_method == "pm"
        assert (await s.scalar(select(AdPost))).status == "canceled"
    mgr, client = manager()
    assert await publisher.publish_one(mgr) is False  # отменённое не публикуется


async def test_auto_approve_only_clean_orders(maker, tgbot):
    await setup_group(maker, price=100)
    await make_user(maker, 70)
    async with maker() as s:
        await settings.put(s, "adpost_auto_approve", "1")
        gid = (await s.scalar(select(Group))).id

    async def submit_text(text):
        async with maker() as s:
            o = await adposts.new_draft(s, 70, text)
            await adposts.toggle_group(s, o, gid)
            await adposts.submit(s, o)
            return o.id

    clean = await submit_text(TEXT)
    dirty = await submit_text("Лучший алкоголь с доставкой по городу, звоните прямо сейчас")
    async with maker() as s:
        assert await adposts.maybe_auto_approve(s, await s.get(AdOrder, clean)) == "approved"  # сразу ждёт оплату
        assert await adposts.maybe_auto_approve(s, await s.get(AdOrder, dirty)) is None  # подозрительный — админу
        assert (await s.get(AdOrder, dirty)).status == "pending"
        await settings.put(s, "adpost_auto_approve", "0")
        c2 = await adposts.new_draft(s, 70, TEXT + " новый")
        await adposts.toggle_group(s, c2, gid)
        await adposts.submit(s, c2)
        assert await adposts.maybe_auto_approve(s, c2) is None  # выключено — всё к админу


async def test_unpaid_orders_get_reminder_then_expire(maker, tgbot):
    await setup_group(maker, price=100)
    await make_user(maker, 70)
    oid = await make_order(maker)
    async with maker() as s:
        o = await s.get(AdOrder, oid)
        await adposts.approve(s, o)
        assert await publisher.nudge_unpaid() == 0  # только что одобрен — рано
        o.decided_at = now() - timedelta(hours=25)
        await s.commit()
    assert await publisher.nudge_unpaid() == 1
    assert any("ждёт оплаты" in t for t in tgbot.texts(70))
    assert await publisher.nudge_unpaid() == 0  # напомнили один раз
    async with maker() as s:
        (await s.get(AdOrder, oid)).decided_at = now() - timedelta(hours=73)
        await s.commit()
    assert await publisher.nudge_unpaid() == 1
    async with maker() as s:
        assert (await s.get(AdOrder, oid)).status == "canceled"
    assert any("отменён: не дождались оплаты" in t for t in tgbot.texts(70))


async def test_buyer_can_view_group_list_before_ordering(maker, tgbot, monkeypatch):
    sent = AsyncMock()
    monkeypatch.setattr(ah, "reply", sent)
    await setup_group(maker, price=500)
    u = await make_user(maker, 70, city="Махачкала")
    await ah.ap_list(cbq("ap:list"), u)
    text = sent.await_args.args[1]
    assert "Барахолка" in text and "5000 уч." in text and "500 ₽" in text and "Всего: 1," in text
    assert "ap:new" in str(sent.await_args.args[2])
    async with maker() as s:
        await settings.put(s, "adpost_enabled", "0")
    off = cbq("ap:list")
    await ah.ap_list(off, u)
    assert off.answer.await_args.kwargs.get("show_alert") is True  # сервис выключен — списка нет


def msg(uid=ADMIN_ID):
    m = MagicMock()
    m.from_user.id = uid
    m.answer = AsyncMock()
    return m


async def test_prices_per_group_and_channel_with_own_publisher(maker, tgbot):
    await setup_group(maker, price=100)
    await make_user(maker, 70)
    async with maker() as s:
        s.add(ScoutAccount(id=2, name="chan_admin", api_id=1, api_hash="x", enabled=True, can_post=True, status="ok"))
        s.add(Group(tg_id=-200, username="newschan", title="Новости", members=9000, status="joined", kind="channel",
                    ads_allowed=True, ad_price=1500, ad_account_id=2))
        # канал без назначенного аккаунта и группа без разрешения не доступны
        s.add(Group(tg_id=-201, username="nochan", title="Без аккаунта", members=50, status="joined", kind="channel", ads_allowed=True))
        await s.commit()
        names = [g.title for g in await adposts.eligible_groups(s)]
        assert names == ["Новости", "Барахолка"]  # группа и канал, отсортированы по участникам
        order = await adposts.new_draft(s, 70, TEXT)
        for g in await adposts.eligible_groups(s):
            await adposts.toggle_group(s, order, g.id)
        await adposts.submit(s, order)
        assert order.price == 1500 + 100  # у канала своя цена, у группы общая
        await adposts.approve(s, order)  # платный → ждёт оплаты; для проверки публикации ставим вручную
        order.price = 0
        await adposts.start(s, order) if order.status == "pending" else None
    # публикация в канал идёт от назначенного аккаунта (админа канала)
    async with maker() as s:
        o = await s.scalar(select(AdOrder))
        o.status = "running"
        for g in await adposts.eligible_groups(s):
            s.add(AdPost(order_id=o.id, group_id=g.id))
        await s.commit()
    chan_client = AsyncMock()
    chan_client.get_entity = AsyncMock(return_value=object())
    chan_client.send_message = AsyncMock(return_value=SimpleNamespace(id=77))
    mgr = SimpleNamespace(clients={2: chan_client, 1: AsyncMock()})
    assert await publisher.publish_one(mgr) is True
    chan_client.send_message.assert_awaited_once()
    async with maker() as s:
        post = await s.scalar(select(AdPost).where(AdPost.status == "sent"))
        assert post.link == "https://t.me/newschan/77"


async def test_admin_sets_group_price_and_order_price(maker, tgbot):
    await setup_group(maker, price=100)
    await make_user(maker, 70)
    async with maker() as s:
        gid = (await s.scalar(select(Group))).id
    m = msg()
    async with maker() as s:
        assert await ah.apg_price(s, m, {"gid": gid}, "двести") is False  # не число — переспросит
        await ah.apg_price(s, m, {"gid": gid}, "750")
        assert (await s.get(Group, gid)).ad_price == 750
    oid = await make_order(maker)
    async with maker() as s:
        assert (await s.get(AdOrder, oid)).price == 750
        await ah.apa_price(s, msg(), {"order_id": oid}, "1200")
        assert (await s.get(AdOrder, oid)).price == 1200  # админ поставил свою цену заказа
        await adposts.approve(s, await s.get(AdOrder, oid))
        with pytest.raises(adposts.AdError):
            await adposts.set_order_price(s, await s.get(AdOrder, oid), 10)  # после одобрения цену не меняем


async def test_reject_and_cancel_require_reason_text_for_buyer(maker, tgbot):
    await setup_group(maker)
    await make_user(maker, 70)
    oid = await make_order(maker)
    async with maker() as s:
        await ah.apa_reason(s, msg(), {"order_id": oid, "op": "no"}, "Нужно название компании и телефон")
        o = await s.get(AdOrder, oid)
        assert o.status == "rejected" and o.reason == "Нужно название компании и телефон"
    assert any("отклонён" in t and "Причина: Нужно название компании" in t for t in tgbot.texts(70))
    oid2 = await make_order(maker)
    async with maker() as s:
        await adposts.approve(s, await s.get(AdOrder, oid2))
        await ah.apa_reason(s, msg(), {"order_id": oid2, "op": "cancel"}, "-")  # без причины
        assert (await s.get(AdOrder, oid2)).status == "canceled"
    assert any("отменён администратором" in t and "Причина" not in t for t in tgbot.texts(70))


async def test_group_card_toggle_and_channel_needs_publisher(maker, tgbot, monkeypatch):
    sent = AsyncMock()
    monkeypatch.setattr(ah, "reply", sent)
    async with maker() as s:
        s.add(ScoutAccount(id=3, name="a3", api_id=1, api_hash="x", enabled=True, can_post=True, status="ok"))
        s.add(Group(tg_id=-300, username="ch", title="Канал", members=10, status="joined", kind="channel"))
        await s.commit()
        gid = (await s.scalar(select(Group))).id
    c = cbq(f"apg:tog:{gid}", uid=ADMIN_ID)
    await ah.groups_toggle(c)
    assert c.answer.await_args.kwargs.get("show_alert") is True  # без аккаунта-публикатора нельзя разрешить
    await ah.group_account_cycle(cbq(f"apg:acc:{gid}", uid=ADMIN_ID))
    await ah.groups_toggle(cbq(f"apg:tog:{gid}", uid=ADMIN_ID))
    async with maker() as s:
        g = await s.get(Group, gid)
        assert g.ad_account_id == 3 and g.ads_allowed
    assert "админом канала" in sent.await_args.args[1]


async def test_per_group_daily_limit(maker, tgbot):
    await setup_group(maker)
    await make_user(maker, 70)
    async with maker() as s:
        await settings.put(s, "adpost_group_daily", "1")
        await settings.put(s, "adpost_gap_min", "1")
        await settings.put(s, "adpost_daily_limit", "50")
        gid = (await s.scalar(select(Group))).id
    ids = []
    for i in range(2):  # два разных заказа в одну группу
        async with maker() as s:
            o = await adposts.new_draft(s, 70, TEXT + str(i))
            await adposts.toggle_group(s, o, gid)
            await adposts.submit(s, o)
            await adposts.approve(s, o)
            ids.append(o.id)
    mgr, client = manager()
    assert await publisher.publish_one(mgr) is True
    async with maker() as s:
        first = await s.scalar(select(AdPost).where(AdPost.status == "sent"))
        first.sent_at = now() - timedelta(minutes=5)  # пауза аккаунта прошла
        await s.commit()
    assert await publisher.publish_one(mgr) is False  # в группу уже вышло 1 из 1 в сутки
    assert client.send_message.await_count == 1
    async with maker() as s:
        assert (await s.scalar(select(func.count()).select_from(AdPost).where(AdPost.status == "queued"))) == 1


async def many_groups(maker):
    async with maker() as s:
        s.add(ScoutAccount(id=1, name="sc", api_id=1, api_hash="x", enabled=True, can_post=True, status="ok"))
        for i, members in enumerate((100, 1000, 5000, 10000)):
            s.add(Group(tg_id=-1000 - i, username=f"g{i}", title=f"Группа {i}", members=members, status="joined",
                        kind="group", account_id=1))
        s.add(Group(tg_id=-2000, username="chan", title="Канал", members=3000, status="joined", kind="channel"))  # без аккаунта
        await s.commit()


async def test_bulk_price_algorithm_and_allow_all(maker, tgbot):
    await many_groups(maker)
    async with maker() as s:
        groups = await adposts.candidates(s)
        prices = adposts.scaled_prices(groups, 300, 2000)
        by_members = {g.members: prices[g.id] for g in groups}
        assert by_members[100] == 300 and by_members[10000] == 2000  # самая маленькая — «от», самая большая — «до»
        assert 300 < by_members[1000] < by_members[3000] < by_members[5000] < 2000  # пропорционально участникам
        assert all(p % 10 == 0 for p in prices.values())
        assert adposts.parse_range("300-2000") == (300, 2000) and adposts.parse_range("300 – 2000") == (300, 2000)
        assert adposts.parse_range("2000-300") is None and adposts.parse_range("500") is None
        assert await adposts.apply_scaled(s, 300, 2000) == 5
        assert (await s.scalar(select(Group).where(Group.members == 10000))).ad_price == 2000
        assert await adposts.set_all_prices(s, 700) == 5
        assert {g.ad_price for g in await adposts.candidates(s)} == {700}
        changed, skipped = await adposts.allow_all(s, True)
        assert changed == 4 and skipped == 1  # канал без аккаунта-публикатора не разрешается
        assert len(await adposts.eligible_groups(s)) == 4
        assert await adposts.allow_all(s, False) == (5, 0) and not await adposts.eligible_groups(s)


async def test_bulk_screens(maker, tgbot, monkeypatch):
    sent = AsyncMock()
    monkeypatch.setattr(ah, "reply", sent)
    await many_groups(maker)
    await ah.groups_for_ads(cbq("apg:list", uid=ADMIN_ID))
    shown = str(sent.await_args.args[2])
    assert "apg:allprice" in shown and "apg:algo" in shown and "apg:allow:1" in shown and "apg:allow:0" in shown
    m = msg()
    async with maker() as s:
        assert await ah.apg_algo(s, m, {}, "много") is False  # непонятный ввод — переспросит
        await ah.apg_algo(s, m, {}, "300-2000")
        assert "apg:algo_ok:300:2000" in str(m.answer.await_args.kwargs["reply_markup"]) and "→ 300 ₽" in m.answer.await_args.args[0]
        assert await ah.apg_allprice(s, msg(), {}, "abc") is False
        await ah.apg_allprice(s, msg(), {}, "800")
    await ah.algo_apply(cbq("apg:algo_ok:300:2000", uid=ADMIN_ID))
    c = cbq("apg:allow:1", uid=ADMIN_ID)
    await ah.allow_all_handler(c)
    assert "Разрешено: 4" in c.answer.await_args.args[0] and "Пропущено" in c.answer.await_args.args[0]


async def test_ads_link_opens_order_screen_without_registration(maker, tgbot, monkeypatch):
    from aiogram.filters import CommandObject

    from app.bot import setup

    sent = AsyncMock()
    monkeypatch.setattr(ah, "reply", sent)
    await setup_group(maker, price=400)
    async with maker() as s:
        s.add(User(tg_id=90, name="Рекламодатель", verified=True, onboarded=False))
        await s.commit()
        user = await s.get(User, 90)
    state = MagicMock()
    state.clear = AsyncMock()
    m = msg(90)
    await setup.start(m, state, user, CommandObject(command="start", args="ads"))
    async with maker() as s:
        u = await s.get(User, 90)
        assert u.onboarded and u.source == "ads"  # ниши и город ему не нужны
    assert "Реклама в группах и каналах" in sent.await_args.args[1] and "ap:new" in str(sent.await_args.args[2])
    m.answer.assert_not_called()  # про ниши не спрашивали
    # команда /ads делает то же самое
    await ah.ads_cmd(msg(90), user)
    assert "ap:list" in str(sent.await_args.args[2])


async def test_ready_message_for_client(maker, tgbot):
    await setup_group(maker, price=500)
    c = cbq("apg:share", uid=ADMIN_ID)
    c.bot.me = AsyncMock(return_value=SimpleNamespace(username="klientradarbot"))
    await ah.share_text(c)
    text = c.message.answer.await_args_list[0].args[0]
    assert "Барахолка — 5000 уч. — 500 ₽" in text and "https://t.me/klientradarbot?start=ads" in text
    assert "название компании" in text  # правило для вакансий сразу в сообщении


async def test_second_payment_for_same_order_is_not_lost(maker, tgbot, monkeypatch):
    from app.models import Payment
    from tests.test_payments import FakeYK

    monkeypatch.setattr(config, "YOOKASSA_SHOP_ID", "1")
    monkeypatch.setattr(config, "YOOKASSA_SECRET_KEY", "k")
    await setup_group(maker, price=300)
    await make_user(maker, 70)
    oid = await make_order(maker)
    async with maker() as s:
        await adposts.approve(s, await s.get(AdOrder, oid))
    yk = FakeYK()
    for _ in range(2):  # покупатель нажал «Оплатить» дважды и оплатил обе ссылки
        async with maker() as s:
            await payments.create_ad_link(s, await s.get(AdOrder, oid), "https://t.me/b", api=yk)
    for pid in ("yk-1", "yk-2"):
        yk.payments[pid].update(status="succeeded", paid=True)
    assert await ph.process_links(yk) == 2
    assert await ph.process_links(yk) == 0
    async with maker() as s:
        pays = (await s.scalars(select(Payment).order_by(Payment.id))).all()
        assert [p.provider_id for p in pays] == ["yk-1", "yk-2"] and all(p.days == 0 for p in pays)
        assert (await s.scalar(select(func.count()).select_from(AdPost))) == 1  # посты поставлены один раз
    assert any("Лишняя оплата" in t for t in tgbot.texts(ADMIN_ID))
    assert any("Деньги вернём" in t for t in tgbot.texts(70))


async def test_deleteme_cancels_ad_orders_and_wipes_texts(maker, tgbot):
    from app.services import erase

    await setup_group(maker)
    await make_user(maker, 70)
    oid = await make_order(maker)
    async with maker() as s:
        await adposts.approve(s, await s.get(AdOrder, oid))
        assert await erase.erase_user(s, 70)
        o = await s.get(AdOrder, oid)
        assert o.status == "canceled" and TEXT not in o.text
        assert (await s.scalar(select(AdPost))).status == "canceled"
    mgr, client = manager()
    assert await publisher.publish_one(mgr) is False


async def test_group_daily_limit_counts_local_day(maker, monkeypatch):
    """Сутки для лимитов рекламы — местные (как у заявок), а не по UTC."""
    from app.services import distribute

    await setup_group(maker)
    start = now() - timedelta(hours=3)  # местные сутки начались 3 часа назад
    monkeypatch.setattr(distribute, "day_start", lambda: start)
    monkeypatch.setattr(adposts, "day_start", lambda: start)
    async with maker() as s:
        g = await s.scalar(select(Group))
        s.add(AdPost(order_id=1, group_id=g.id, status="sent", sent_at=start - timedelta(minutes=5)))  # вчера
        s.add(AdPost(order_id=1, group_id=g.id, status="sent", sent_at=start + timedelta(minutes=5)))  # сегодня
        await s.commit()
        assert await adposts.group_sent_today(s, g.id) == 1


async def test_double_approve_publishes_once(maker, tgbot):
    """Двойное нажатие «✅ Одобрить» (или одобрение из двух сообщений) не ставит посты дважды."""
    await setup_group(maker)
    await make_user(maker, 70)
    oid = await make_order(maker)
    async with maker() as s1, maker() as s2:
        a, b = await s1.get(AdOrder, oid), await s2.get(AdOrder, oid)  # оба видят «на проверке»
        await adposts.approve(s1, a)
        with pytest.raises(adposts.AdError):
            await adposts.approve(s2, b)
    async with maker() as s:
        assert await s.scalar(select(func.count()).select_from(AdPost)) == 1


async def test_payment_after_cancel_is_not_started(maker, tgbot):
    await setup_group(maker, price=500)
    await make_user(maker, 70)
    oid = await make_order(maker)
    async with maker() as s:
        await adposts.approve(s, await s.get(AdOrder, oid))
    async with maker() as s1, maker() as s2:
        stale = await s1.get(AdOrder, oid)  # оплата пришла, а заказчик в этот момент отменил
        await adposts.cancel(s2, await s2.get(AdOrder, oid))
        assert stale.status == "approved"
        assert await adposts.mark_paid(s1, oid, "yk-late", 50000) is None  # уйдёт в «лишнюю оплату» с возвратом
    async with maker() as s:
        assert await s.scalar(select(func.count()).select_from(AdPost)) == 0


async def test_closed_group_finishes_order_and_tells_admin(maker, tgbot):
    await setup_group(maker)
    await make_user(maker, 70)
    oid = await make_order(maker)
    async with maker() as s:
        await adposts.approve(s, await s.get(AdOrder, oid))
        (await s.scalar(select(Group))).ads_allowed = False  # админ закрыл группу после заказа
        await s.commit()
    mgr, _ = manager()
    await publisher.publish_one(mgr)
    async with maker() as s:
        assert (await s.get(AdOrder, oid)).status == "done"  # не висит «публикуется» вечно
    assert any("закрыта для рекламы" in t for t in tgbot.texts(ADMIN_ID))
    assert any("выполнен" in t for t in tgbot.texts(70))


async def test_no_retry_after_refund_or_cancel(maker, tgbot):
    await setup_group(maker)
    await make_user(maker, 70)
    oid = await make_order(maker)
    async with maker() as s:
        order = await s.get(AdOrder, oid)
        await adposts.cancel(s, order)
        with pytest.raises(adposts.AdError):
            await adposts.retry_failed(s, order)


def test_rubles_input():
    assert adposts.rubles(" 1500 ") == 1500
    assert adposts.rubles("²") is None and adposts.rubles("9" * 5000) is None and adposts.rubles("2000000") is None


async def test_photo_ad_published_with_caption(maker, tgbot, tmp_path, monkeypatch):
    """Реклама картинкой с подписью: принимается и публикуется картинкой."""
    monkeypatch.setattr(config, "MEDIA_DIR", str(tmp_path))
    await setup_group(maker)
    await make_user(maker, 70)
    name = adposts.new_photo_name()
    adposts.media_path(name).write_bytes(b"jpg")
    async with maker() as s:
        with pytest.raises(adposts.AdError, match="подпись"):
            await adposts.new_draft(s, 70, "", photo=name)
        with pytest.raises(adposts.AdError, match="800"):
            await adposts.new_draft(s, 70, "я" * 850, photo=name)
        order = await adposts.new_draft(s, 70, TEXT, photo=name)
        await adposts.toggle_group(s, order, (await s.scalar(select(Group))).id)
        await adposts.submit(s, order)
        await adposts.approve(s, order)
    mgr, client = manager()
    client.send_file = AsyncMock(return_value=SimpleNamespace(id=777))
    assert await publisher.publish_one(mgr) is True
    client.send_file.assert_awaited_once()
    assert client.send_file.call_args.kwargs["caption"].startswith(TEXT)
    client.send_message.assert_not_awaited()


async def test_photo_input_and_non_text_answer(maker, tgbot, tmp_path, monkeypatch):
    from app.bot.handlers import inputs

    monkeypatch.setattr(config, "MEDIA_DIR", str(tmp_path))
    await setup_group(maker)
    await make_user(maker, 70)

    async def fake_download(file, destination):
        destination.write_bytes(b"jpg")

    state = MagicMock()
    state.get_data, state.clear = AsyncMock(return_value={"action": "ap_text"}), AsyncMock()
    m = MagicMock()
    m.from_user.id, m.caption, m.media_group_id = 70, TEXT, None
    m.photo = [SimpleNamespace(file_id="small"), SimpleNamespace(file_id="big")]
    m.answer = AsyncMock()
    m.bot.download = AsyncMock(side_effect=fake_download)
    await inputs.on_media(m, state)
    async with maker() as s:
        order = await s.scalar(select(AdOrder))
        assert order.photo and adposts.media_path(order.photo).exists() and order.text == TEXT
    state.clear.assert_awaited()
    # голосовое/стикер во время любого вопроса — бот не молчит
    state.get_data = AsyncMock(return_value={"action": "client_note"})
    m2 = MagicMock()
    m2.photo, m2.answer = None, AsyncMock()
    await inputs.on_media(m2, state)
    assert "текстом" in m2.answer.call_args.args[0]


async def test_group_hours_window(maker, tgbot, monkeypatch):
    """У группы часы «9-21»: вне их разведчик ждёт, в них — публикует."""
    from app.services import quiet

    await setup_group(maker)
    await make_user(maker, 70)
    oid = await make_order(maker)
    async with maker() as s:
        await adposts.approve(s, await s.get(AdOrder, oid))
        (await s.scalar(select(Group))).ad_hours = "9-21"
        await s.commit()
    mgr, client = manager()
    monkeypatch.setattr(quiet, "local_now", lambda: now().replace(hour=23))
    assert await publisher.publish_one(mgr) is False
    monkeypatch.setattr(quiet, "local_now", lambda: now().replace(hour=12))
    assert await publisher.publish_one(mgr) is True
    client.send_message.assert_awaited_once()


async def test_deleted_post_detected(maker, tgbot):
    """Пост удалили правила/бот группы: через 15 минут бот это видит, выключает рекламу в группе и сообщает."""
    await setup_group(maker)
    await make_user(maker, 70)
    oid = await make_order(maker)
    async with maker() as s:
        await adposts.approve(s, await s.get(AdOrder, oid))
    mgr, client = manager()
    await publisher.publish_one(mgr)
    assert await publisher.check_posts(mgr) == 0  # 15 минут ещё не прошло
    async with maker() as s:
        (await s.scalar(select(AdPost))).sent_at = now() - timedelta(minutes=16)
        await s.commit()
    client.get_messages = AsyncMock(return_value=None)  # сообщения больше нет
    assert await publisher.check_posts(mgr) == 1
    async with maker() as s:
        post = await s.scalar(select(AdPost))
        assert post.status == "deleted" and post.checked
        assert not (await s.scalar(select(Group))).ads_allowed
    assert any("удалили" in t for t in tgbot.texts(ADMIN_ID)) and any("удалили" in t for t in tgbot.texts(70))
    assert await publisher.check_posts(mgr) == 0  # второй раз не проверяем


async def test_group_hours_and_note_inputs(maker, tgbot):
    await setup_group(maker)
    m = MagicMock()
    m.answer = AsyncMock()
    async with maker() as s:
        gid = (await s.scalar(select(Group))).id
        assert await ah.apg_hours(s, m, {"gid": gid}, "25-3") is False
        await ah.apg_hours(s, m, {"gid": gid}, "9 - 21")
        await ah.apg_note(s, m, {"gid": gid}, "подписка на @kanal — сделано")
    async with maker() as s:
        g = await s.scalar(select(Group))
        assert g.ad_hours == "9-21" and "подписка" in g.ad_note


def test_split_price_sums_exactly():
    assert adposts.split_price(1000, [300, 300, 400]) == [300, 300, 400]
    assert sum(adposts.split_price(1000, [1, 1, 1])) == 1000
    assert adposts.split_price(500, [0, 0]) == [250, 250]


async def test_partial_finish_refunds_unpublished_part(maker, tgbot, monkeypatch):
    """Вышло в одной группе из двух: «✂️ Выполнен частично» снимает очередь и возвращает долю второй группы."""
    monkeypatch.setattr(config, "YOOKASSA_SHOP_ID", "1")
    monkeypatch.setattr(config, "YOOKASSA_SECRET_KEY", "k")
    from app.models import Payment

    await setup_group(maker, price=300)
    await make_user(maker, 70)
    async with maker() as s:
        s.add(Group(tg_id=-102, username="g3", title="Вторая", members=100, status="joined", kind="group",
                    account_id=1, ads_allowed=True, ad_price=700))
        await s.commit()
        order = await adposts.new_draft(s, 70, TEXT)
        for g in (await s.scalars(select(Group))).all():
            await adposts.toggle_group(s, order, g.id)
        await adposts.submit(s, order)
        assert order.price == 1000
        await adposts.approve(s, order)
        await adposts.mark_paid(s, order.id, "yk-9", 100000)
        oid = order.id
    mgr, client = manager()
    await publisher.publish_one(mgr)  # первый пост вышел, второй ждёт паузы
    calls = []

    async def fake_refund(pid, amount, cur, key):
        calls.append((amount, key))
        return {"status": "succeeded"}

    monkeypatch.setattr(payments, "_yookassa_refund", fake_refund)
    async with maker() as s:
        order = await s.get(AdOrder, oid)
        sent_group = (await s.scalar(select(AdPost).where(AdPost.status == "sent"))).group_id
        expected = 700 if (await s.get(Group, sent_group)).ad_price != 700 else 300
        assert await adposts.unpublished_value(s, order) == expected
    c = MagicMock()
    c.data, c.from_user.id = f"apa:pok:{oid}:1", ADMIN_ID
    c.answer, c.message.edit_text, c.message.answer = AsyncMock(), AsyncMock(), AsyncMock()
    monkeypatch.setattr(ah, "reply", AsyncMock())
    await ah.partial_do(c)
    async with maker() as s:
        order = await s.get(AdOrder, oid)
        pay = await s.scalar(select(Payment))
        assert order.status == "done" and pay.refunded_amount == expected * 100 and not pay.refunded
        assert (await s.scalar(select(AdPost).where(AdPost.status == "canceled"))) is not None
        assert await adposts.unpublished_value(s, order) == 0  # второй раз вернуть то же нельзя
    assert calls == [(expected * 100, f"refund-{pay.id}-part-0")]
    assert any("выполнен частично" in t and f"вернули {expected}" in t for t in tgbot.texts(70))


async def test_failed_group_switched_off_and_no_media(maker, tgbot):
    """Аккаунт забанен в группе — группа сама уходит из списка для покупателей; картинки запрещены — только текст."""
    from telethon.errors import ChatSendPhotosForbiddenError, UserBannedInChannelError

    await setup_group(maker)
    await make_user(maker, 70)
    oid = await make_order(maker)
    async with maker() as s:
        await adposts.approve(s, await s.get(AdOrder, oid))
    mgr, _ = manager(send=AsyncMock(side_effect=UserBannedInChannelError(request=None)))
    await publisher.publish_one(mgr)
    async with maker() as s:
        assert not (await s.scalar(select(Group))).ads_allowed
        assert await adposts.eligible_groups(s) == []
        g = await s.scalar(select(Group))
        g.ads_allowed, g.no_media = True, True
        await s.commit()
        assert len(await adposts.eligible_groups(s)) == 1
        assert await adposts.eligible_groups(s, photo=True) == []  # с картинкой туда не предлагаем
    assert ChatSendPhotosForbiddenError  # (ошибка «картинки запрещены» помечает группу no_media)


async def test_auto_refund_when_order_finishes(maker, tgbot, monkeypatch):
    """Пост не вышел (бан в группе) → заказ закончен → деньги за эту группу вернулись сами, обоим пришло сообщение."""
    monkeypatch.setattr(config, "YOOKASSA_SHOP_ID", "1")
    monkeypatch.setattr(config, "YOOKASSA_SECRET_KEY", "k")
    from telethon.errors import UserBannedInChannelError

    from app.models import Payment

    await setup_group(maker, price=400)
    await make_user(maker, 70)
    oid = await make_order(maker)
    async with maker() as s:
        await adposts.approve(s, await s.get(AdOrder, oid))
        await adposts.mark_paid(s, oid, "yk-5", 40000)
    calls = []

    async def fake_refund(pid, amount, cur, key):
        calls.append(amount)
        return {"status": "succeeded"}

    monkeypatch.setattr(payments, "_yookassa_refund", fake_refund)
    mgr, _ = manager(send=AsyncMock(side_effect=UserBannedInChannelError(request=None)))
    await publisher.publish_one(mgr)
    async with maker() as s:
        assert (await s.get(AdOrder, oid)).status == "done"
        pay = await s.scalar(select(Payment))
        assert pay.refunded_amount == 40000 and pay.refunded
        assert (await s.scalar(select(AdPost))).settled
    assert calls == [40000]
    assert any("вернули 400" in t for t in tgbot.texts(70)) and any("автоматически вернули 400" in t for t in tgbot.texts(ADMIN_ID))
    # повторный итог (или проверка) ничего второй раз не вернёт
    await publisher.notify_done(oid)
    assert calls == [40000]


async def test_auto_refund_off_tells_admin(maker, tgbot, monkeypatch):
    from telethon.errors import UserBannedInChannelError

    await setup_group(maker, price=400)
    await make_user(maker, 70)
    oid = await make_order(maker)
    async with maker() as s:
        await settings.put(s, "adpost_auto_refund", "0")
        await adposts.approve(s, await s.get(AdOrder, oid))
        await adposts.mark_paid(s, oid, "yk-6", 40000)
    mgr, _ = manager(send=AsyncMock(side_effect=UserBannedInChannelError(request=None)))
    await publisher.publish_one(mgr)
    assert any("к возврату 400" in t for t in tgbot.texts(ADMIN_ID))
    assert any("администратор вернёт" in t for t in tgbot.texts(70))


async def test_probe_group_rights(maker, tgbot):
    from telethon.tl.types import ChatAdminRights, ChatBannedRights

    await setup_group(maker)
    mgr, client = manager()
    async with maker() as s:
        g = await s.scalar(select(Group))
    closed = SimpleNamespace(left=False, broadcast=False, creator=False, admin_rights=None, banned_rights=None,
                             default_banned_rights=ChatBannedRights(until_date=None, send_messages=True))
    client.get_entity = AsyncMock(return_value=closed)
    assert (await publisher.probe(mgr, g))[0] == "warn"
    nopics = SimpleNamespace(left=False, broadcast=False, creator=False, admin_rights=None, banned_rights=None,
                             default_banned_rights=ChatBannedRights(until_date=None, send_media=True))
    client.get_entity = AsyncMock(return_value=nopics)
    level, text, no_media, closed = await publisher.probe(mgr, g)
    assert level == "warn" and no_media and "картинки" in text and not closed
    channel = SimpleNamespace(left=False, broadcast=True, creator=False, admin_rights=None)
    client.get_entity = AsyncMock(return_value=channel)
    assert (await publisher.probe(mgr, g))[0] == "bad"
    channel.admin_rights = ChatAdminRights(post_messages=True)
    assert (await publisher.probe(mgr, g))[0] == "ok"
    client.get_entity = AsyncMock(return_value=SimpleNamespace(left=True))
    assert await publisher.check_group(mgr, g.id) == ("bad", "аккаунт не состоит в группе")
    async with maker() as s:
        g = await s.scalar(select(Group))
        assert not g.ads_allowed and g.ad_check.startswith("⛔")


async def test_estimate_text(maker):
    await setup_group(maker)
    async with maker() as s:
        await settings.put(s, "adpost_daily_limit", "2")
        for i in range(3):
            s.add(Group(tg_id=-200 - i, username=f"e{i}", title=f"E{i}", members=10, status="joined", kind="group", account_id=1, ads_allowed=True))
        await s.commit()
        ids = list((await s.scalars(select(Group.id))).all())
        assert "2 дн" in await adposts.estimate(s, ids)
        assert await adposts.estimate(s, ids[:1]) == "в течение часа"


async def test_closed_group_locked_for_buyers_and_reopens(maker, tgbot):
    """Группа закрыта сейчас: покупатель видит её с 🔒, но выбрать не может; открылась — снова доступна;
    закрыта больше 2 суток — реклама в ней выключается сама."""
    from telethon.tl.types import ChatBannedRights

    await setup_group(maker)
    await make_user(maker, 70)
    mgr, client = manager()
    closed = SimpleNamespace(left=False, broadcast=False, creator=False, admin_rights=None, banned_rights=None,
                             default_banned_rights=ChatBannedRights(until_date=None, send_messages=True))
    client.get_entity = AsyncMock(return_value=closed)
    async with maker() as s:
        gid = (await s.scalar(select(Group))).id
    assert (await publisher.recheck_groups(mgr)) == []  # ни разу не проверенную — проверили фоном
    async with maker() as s:
        assert (await s.get(Group, gid)).ad_closed_at is not None
        assert await adposts.eligible_groups(s) == [] and len(await adposts.closed_groups(s)) == 1
        order = await adposts.new_draft(s, 70, TEXT)
        order.group_ids = str(gid)  # выбрал, пока была открыта
        await s.commit()
    # открылась
    client.get_entity = AsyncMock(return_value=SimpleNamespace(left=False, broadcast=False, creator=False, admin_rights=None,
                                                               banned_rights=None, default_banned_rights=None))
    async with maker() as s:
        (await s.get(Group, gid)).ad_checked_at = now() - timedelta(hours=3)
        await s.commit()
    await publisher.recheck_groups(mgr)
    async with maker() as s:
        assert (await s.get(Group, gid)).ad_closed_at is None and len(await adposts.eligible_groups(s)) == 1
    # снова закрыта, и уже больше 2 суток
    client.get_entity = AsyncMock(return_value=closed)
    async with maker() as s:
        g = await s.get(Group, gid)
        g.ad_closed_at, g.ad_checked_at = now() - timedelta(hours=49), now() - timedelta(hours=3)
        await s.commit()
    assert await publisher.recheck_groups(mgr) == ["Барахолка"]
    async with maker() as s:
        assert not (await s.get(Group, gid)).ads_allowed
    assert any("выключена" in t for t in tgbot.texts(ADMIN_ID))


async def test_picker_drops_closed_selected_group(maker, tgbot, monkeypatch):
    await setup_group(maker)
    await make_user(maker, 70)
    async with maker() as s:
        g = await s.scalar(select(Group))
        order = await adposts.new_draft(s, 70, TEXT)
        order.group_ids = str(g.id)
        g.ad_closed_at = now()
        await s.commit()
        shown = AsyncMock()
        monkeypatch.setattr(ah, "reply", shown)
        await ah.show_picker(MagicMock(), s, order, "")
        assert order.group_ids == ""
    buttons = [b.callback_data for row in shown.call_args.args[2].inline_keyboard for b in row]
    assert f"ap:locked:{g.id}" in buttons and f"ap:t:{order.id}:{g.id}" not in buttons
