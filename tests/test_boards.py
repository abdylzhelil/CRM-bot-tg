"""📊 Рейтинг заявок: закреп в группах с ботом, комиссия админу кредитами 💰, рейтинг в боте (подробно — на 💎)."""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from app.models import Category, Group, Lead, User, now
from app.services import boards, payments, settings


async def _data(maker):
    async with maker() as s:
        root = Category(name="Ремонт", emoji="🔨")
        s.add(root)
        await s.flush()
        sub = Category(name="Сантехники", parent_id=root.id)
        s.add(sub)
        await s.flush()
        for i, (city, at) in enumerate([("Махачкала", now()), ("Махачкала", now() - timedelta(days=3)),
                                        ("Москва", now()), ("", now())]):
            s.add(Lead(author_id=100 + i, text=f"Нужен сантехник срочно {i}", text_hash=f"h{i}", category_id=sub.id,
                       city=city, region="Дагестан" if city == "Махачкала" else ("Москва" if city else "Дагестан"),
                       created_at=at))
        g = Group(tg_id=-500, title="Барахолка Махачкала", status="joined", city="Махачкала", region="Дагестан",
                  bot_member=True, board_admin=777)
        s.add(g)
        s.add(User(tg_id=777, name="Админ группы", onboarded=True))
        await s.commit()
        return g.id, root.id


async def test_counts_by_city_and_text(maker):
    gid, root_id = await _data(maker)
    async with maker() as s:
        items, day, week = await boards.counts(s, "Махачкала", "Дагестан")
        assert items[0][0] == root_id and items[0][2] == 3 and day == 2 and week == 3  # с заявкой «только регион»
        g = await s.get(Group, gid)
        body = await boards.group_text(s, g)
    assert "Махачкала" in body and "🔨 Ремонт — 3" in body and "100" not in body  # авторов не показываем
    assert boards.join_link("klientradarbot", gid) == f"https://t.me/klientradarbot?start=src_g{gid}"


async def test_commission_to_group_admin(maker, tgbot):
    gid, _ = await _data(maker)
    async with maker() as s:
        s.add(User(tg_id=888, name="Покупатель", onboarded=True, source=f"g{gid}"))
        await s.commit()
        await settings.put(s, "board_percent", "20")
    async with maker() as s:
        new, _ = await payments.apply(s, 888, 30, 49000, "RUB", "ch-1")
    assert new
    async with maker() as s:
        admin = await s.get(User, 777)
        g = await s.get(Group, gid)
    # 20% от 490 ₽ — кредитами, не днями; первые 3 дня (срок возврата) заморожены
    assert admin.credits_hold == 98 and admin.credits == 0 and g.board_credits == 98 and admin.plan != "full"
    assert any("98 кредитов" in t for t in tgbot.texts(777))
    async with maker() as s:  # повтор того же платежа — второй раз не начисляем
        new, _ = await payments.apply(s, 888, 30, 49000, "RUB", "ch-1")
        assert not new and (await s.get(User, 777)).credits_hold == 98


async def test_group_board_posts_and_pins(maker):
    from app.bot.handlers import board

    gid, _ = await _data(maker)
    bot = MagicMock()
    bot.me = AsyncMock(return_value=SimpleNamespace(id=1, username="klientradarbot"))
    bot.get_chat_member = AsyncMock(return_value=SimpleNamespace(status="administrator", can_pin_messages=True))
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=55))
    bot.pin_chat_message = AsyncMock()
    assert await board.update_one(bot, gid) == ""
    bot.pin_chat_message.assert_awaited()
    async with maker() as s:
        g = await s.get(Group, gid)
    assert g.board_msg_id == 55 and g.board_at is not None
    # бот не админ — молчит, причина видна
    bot.get_chat_member = AsyncMock(return_value=SimpleNamespace(status="member"))
    bot.send_message.reset_mock()
    bot.edit_message_text = AsyncMock()
    assert "не админ" in await board.update_one(bot, gid)
    bot.send_message.assert_not_awaited()


async def test_in_bot_rating_details_only_paid(maker):
    from app.bot.handlers import board

    _, root_id = await _data(maker)
    free = SimpleNamespace(tg_id=5, city="Махачкала", plan="free", is_admin=False)
    cb = MagicMock()
    cb.data, cb.answer = f"rt:n:{root_id}", AsyncMock()
    await board.rating_niche(cb, free)
    assert "полном тарифе" in cb.answer.call_args.args[0]
    full = SimpleNamespace(tg_id=6, city="Махачкала", plan="full", is_admin=False)
    cb.answer.reset_mock()
    await board.rating_niche(cb, full)
    assert "Нужен сантехник" in cb.answer.call_args.args[0]


async def test_offer_auto_marks(maker):
    from app.services import botoffer

    async with maker() as s:
        s.add_all([Group(tg_id=-601, title="A", status="joined", admins="x", bot_member=True),
                   Group(tg_id=-602, title="B", status="joined", admins="y", offer_status="sent",
                         offer_at=now() - timedelta(days=8))])
        await s.commit()
        assert await botoffer.sweep(s) == 2
        await s.commit()
        rows = {g.title: g.offer_status for g in (await s.scalars(__import__("sqlalchemy").select(Group))).all()}
    assert rows == {"A": "bot", "B": "skip"}


async def test_rating_opens_in_one_tap(maker):
    """Владелец 24.10: «почему нажимаешь — сразу к рейтингу не перекидывает»: кнопка в меню и ссылка из группы."""
    from app.bot.common import USER_MENU
    from app.bot.handlers import board

    assert "🏆 Рейтинг" in USER_MENU
    gid, _ = await _data(maker)
    assert board.group_of(f"src_g{gid}") == gid and board.group_of("src_site") == 0
    m = MagicMock()
    m.answer = AsyncMock()
    user = SimpleNamespace(tg_id=9, city="", plan="free", is_admin=False, onboarded=False)
    await board.rating_view(m, user, group_id=gid)
    body = m.answer.call_args.args[0]
    assert "Махачкала" in body and "выберите свои ниши" in body  # место группы, а не пустой город новичка


async def test_take_lead_from_rating(maker, tgbot):
    """Владелец 25.10: «как получать этих клиентов»: номер под заявкой → обычная карточка с «Ответить» (на 💎)."""
    from sqlalchemy import select

    from app.bot.handlers import board
    from app.models import Delivery

    _, root_id = await _data(maker)
    async with maker() as s:
        lead = await s.scalar(select(Lead).where(Lead.city == "Махачкала"))
        s.add(User(tg_id=31, name="Мастер", onboarded=True, plan="full", city="Махачкала"))
        await s.commit()
        u = await s.get(User, 31)
    cb = MagicMock()
    cb.data, cb.answer = f"rt:t:{lead.id}", AsyncMock()
    await board.rating_take(cb, u)
    async with maker() as s:
        assert await s.scalar(select(Delivery).where(Delivery.lead_id == lead.id, Delivery.user_id == 31))
    assert any("сантехник" in t for t in tgbot.texts(31))
    free = SimpleNamespace(tg_id=32, plan="free", is_admin=False)
    cb.answer.reset_mock()
    await board.rating_take(cb, free)
    assert "полном тарифе" in cb.answer.call_args.args[0]


async def test_niche_buttons_in_pin_and_one_tap_join(maker, tgbot):
    """Владелец 25.10: в закрепе — кнопка на каждую нишу; нажал — ниша и город группы уже выбраны, пробные дни, заявки."""
    from app.bot.handlers import board
    from app.models import UserCategory
    from app.services import referrals

    gid, root_id = await _data(maker)
    bot = MagicMock()
    bot.me = AsyncMock(return_value=SimpleNamespace(id=1, username="klientradarbot"))
    bot.get_chat_member = AsyncMock(return_value=SimpleNamespace(status="administrator", can_pin_messages=True))
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=55))
    bot.pin_chat_message = AsyncMock()
    await board.update_one(bot, gid)
    markup = bot.send_message.call_args.kwargs["reply_markup"]
    urls = [b.url for row in markup.inline_keyboard for b in row]
    assert f"https://t.me/klientradarbot?start=src_g{gid}_c{root_id}" in urls and urls[-1].endswith(f"src_g{gid}")
    payload = f"src_g{gid}_c{root_id}"
    assert board.group_of(payload) == gid and board.niche_of(payload) == root_id
    assert referrals.parse_source(payload) == f"g{gid}"  # комиссия админу группы всё равно считается
    async with maker() as s:
        s.add(User(tg_id=4040, name="Новичок", source=f"g{gid}"))
        await s.commit()
        u = await s.get(User, 4040)
    m = MagicMock()
    m.answer = AsyncMock()
    assert await board.quick_join(m, u, payload)
    async with maker() as s:
        row = await s.get(User, 4040)
        assert row.onboarded and row.city == "Махачкала" and row.plan == "full" and row.on_trial
        assert await s.get(UserCategory, (4040, root_id)) is not None
    assert "Всё настроил" in m.answer.call_args_list[0].args[0]
    assert not await board.quick_join(m, row, payload)  # уже зарегистрирован — обычный рейтинг
