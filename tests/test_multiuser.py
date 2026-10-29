"""Многопользовательский режим: раздача заявок, лимиты, изоляция данных, спам-голоса, регистрация."""
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy import func, select, text

from app.bot import notify
from app.bot.handlers import clients, leads
from app.models import BlacklistEntry, Client, Delivery, Lead, User, UserNegative, now
from app.services import distribute, geo, intake
from app.services.antispam import AuthorInfo
from app.services.pipeline import Incoming
from tests.conftest import ADMIN_ID, make_user

_ids = iter(range(1000, 100000))


def msg(text, author=None, group="Бизнес чат"):
    a = AuthorInfo(user_id=author or next(_ids), username="client", first_name="Али")
    return Incoming(text=text, author=a, group_tg_id=-100777, group_title=group, message_id=5)


async def deliveries(factory, lead_id=None):
    async with factory() as s:
        q = select(Delivery)
        if lead_id:
            q = q.where(Delivery.lead_id == lead_id)
        return {d.user_id: d for d in (await s.scalars(q)).all()}


def test_geo_matching():
    assert geo.matches("Махачкала", False, "Махачкала", "Дагестан")
    assert not geo.matches("Махачкала", False, "Москва", "Москва")
    assert geo.matches("Махачкала", False, "", "Дагестан")  # указан только регион
    assert geo.matches("Дагестан", False, "Каспийск", "Дагестан")  # выбран весь регион
    assert geo.matches("Махачкала", False, "", "")  # без адреса — приходит…
    assert not geo.matches("Махачкала", True, "", "")  # …если не включено «только мой город»
    assert geo.matches("", False, "Москва", "Москва")  # вся Россия


async def test_distribution_by_niche_city_plan(maker, tgbot):
    await make_user(maker, 10, city="Махачкала", plan="full")
    await make_user(maker, 11, city="Москва", plan="full")
    await make_user(maker, 12, niches=("Дизайн",), plan="full")
    await make_user(maker, 13)  # бесплатный, вся Россия
    lead = await intake.handle(msg("Ищу кто сделает лендинг в Махачкале"))
    assert lead.city == "Махачкала"
    got = await deliveries(maker, lead.id)
    assert set(got) == {ADMIN_ID, 10, 13}
    assert got[13].deliver_after > now() + timedelta(minutes=20)  # бесплатный — с задержкой
    # мгновенно отправлено админу и платному, бесплатному — нет
    assert tgbot.texts(10) and tgbot.texts(ADMIN_ID) and not tgbot.texts(13)
    async with maker() as s:
        (await s.get(Delivery, got[13].id)).deliver_after = now() - timedelta(seconds=1)
        await s.commit()
    await notify.flush_due()
    assert tgbot.texts(13) and "Махачкала" in tgbot.texts(13)[0]


async def test_cap_paid_first_admin_extra(maker, tgbot):
    for uid in range(20, 28):
        await make_user(maker, uid, plan="full")
    for uid in range(30, 33):
        await make_user(maker, uid)
    lead = await intake.handle(msg("Нужен сайт для магазина, кто сделает?"))
    got = await deliveries(maker, lead.id)
    assert ADMIN_ID in got
    others = set(got) - {ADMIN_ID}
    assert len(others) == 5 and others <= set(range(20, 28))  # лимит 5, платные первыми


async def test_free_daily_limit(maker, tgbot):
    await make_user(maker, 40)
    for i in range(5):
        await intake.handle(msg(f"Ищу разработчика сайта номер {i} для своей компании {i * 7}"))
    lead = await intake.handle(msg("Нужен лендинг срочно, кто сделает?"))
    assert 40 not in await deliveries(maker, lead.id)


async def test_personal_negatives(maker, tgbot):
    await make_user(maker, 50, plan="full")
    async with maker() as s:
        s.add(UserNegative(user_id=50, word="интернет-магазин"))
        await s.commit()
    lead = await intake.handle(msg("Ищу кто сделает интернет-магазин"))
    assert 50 not in await deliveries(maker, lead.id)


async def test_quiet_hours_hold_regular_not_hot(maker, tgbot, monkeypatch):
    await make_user(maker, 60, plan="full", quiet_hours="0-23")
    monkeypatch.setattr(distribute.quiet, "local_now", lambda: now().replace(hour=3))
    calm = await intake.handle(msg("Подскажите кто делает сайты"))
    assert not tgbot.texts(60) and not (await deliveries(maker, calm.id))[60].sent
    await intake.handle(msg("Срочно нужен лендинг, бюджет 20к"))
    assert len(tgbot.texts(60)) == 1 and "🔥" in tgbot.texts(60)[0]


def cb(data, uid):
    c = MagicMock()
    c.data = data
    c.from_user.id = uid
    c.answer, c.message.edit_reply_markup, c.message.answer = AsyncMock(), AsyncMock(), AsyncMock()
    return c


async def test_isolation_and_take(maker, tgbot):
    u = await make_user(maker, 70, plan="full")
    stranger = await make_user(maker, 71, niches=("Дизайн",))
    lead = await intake.handle(msg("Ищу кто сделает лендинг"))
    d = (await deliveries(maker, lead.id))[70]

    other = cb(f"lead:take:{d.id}", 71)
    await leads.lead_action(other, stranger)
    other.answer.assert_called_with("Заявка не найдена", show_alert=True)

    await leads.lead_action(cb(f"lead:take:{d.id}", 70), u)
    async with maker() as s:
        client = await s.scalar(select(Client))
        assert client.user_id == 70 and (await s.get(Delivery, d.id)).status == "taken"
    view = cb("cl:list:all", 71)
    await clients.show_list(view, "all")
    assert "пусто" in view.answer.call_args.args[0].lower()


async def test_spam_votes(maker, tgbot):
    voters = [await make_user(maker, uid, plan="full") for uid in (80, 81, 82)]
    author = 5555
    lead = await intake.handle(msg("Ищу кто сделает лендинг", author=author))
    got = await deliveries(maker, lead.id)
    await leads.lead_action(cb(f"lead:spam:{got[80].id}", 80), voters[0])
    async with maker() as s:
        assert await s.get(BlacklistEntry, author) is None
    # проголосовавший больше не получает этого автора, остальные — получают
    lead2 = await intake.handle(msg("Срочно нужен сайт визитка, кто возьмётся", author=author))
    got2 = await deliveries(maker, lead2.id)
    assert 80 not in got2 and (81 in got2 or 82 in got2)  # заявки делятся поровну: достаётся одному из двоих
    await leads.lead_action(cb(f"lead:spam:{got[81].id}", 81), voters[1])
    await leads.lead_action(cb(f"lead:spam:{got[82].id}", 82), voters[2])
    async with maker() as s:
        assert (await s.get(BlacklistEntry, author)).status == "spam"


async def test_preview_does_not_spend_free_limit(maker, tgbot):
    await intake.handle(msg("Ищу кто сделает лендинг"))
    async with maker() as s:
        u = User(tg_id=90, onboarded=True)
        s.add(u)
        await s.commit()
    await make_user(maker, 91)
    async with maker() as s:
        u = await s.get(User, 91)
        shown = await distribute.preview(s, u)
        assert len(shown) == 1
        assert await distribute.delivered_today(s, 91) == 0


async def test_blocked_user_marked(maker, tgbot):
    await make_user(maker, 95, plan="full")
    tgbot.blocked.add(95)
    await intake.handle(msg("Ищу кто сделает лендинг"))
    async with maker() as s:
        assert (await s.get(User, 95)).blocked


async def test_schema_upgrade_adds_columns():
    from sqlalchemy import pool
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.migrate import upgrade_schema
    from app.models import Base

    engine = create_async_engine("sqlite+aiosqlite://", poolclass=pool.StaticPool)
    async with engine.begin() as conn:
        await conn.execute(text("CREATE TABLE clients (id INTEGER PRIMARY KEY, name VARCHAR(255))"))
        await conn.execute(text("INSERT INTO clients (id, name) VALUES (1, 'Старый')"))
        await conn.run_sync(Base.metadata.create_all)
    await upgrade_schema(engine)
    async with engine.connect() as conn:
        row = (await conn.execute(text("SELECT name, user_id, stage FROM clients"))).one()
    assert row == ("Старый", 0, "new")
    await engine.dispose()


async def test_admin_count(maker):
    async with maker() as s:
        admin = await s.get(User, ADMIN_ID)
        assert admin.is_admin and admin.plan == "full"
        assert await s.scalar(select(func.count()).select_from(Lead)) == 0


async def test_flush_concurrent_sends_once_and_retries_on_flood(maker, tgbot, monkeypatch):
    """Два прохода раздачи одновременно не шлют одну заявку дважды; «подождите» от Telegram — не потеря заявки."""
    import asyncio

    from aiogram.exceptions import TelegramRetryAfter
    from aiogram.methods import SendMessage

    await make_user(maker, 40, plan="full")
    async with maker() as s:
        lead = Lead(group_id=None, group_title="Г", author_id=555, text="Нужен сайт", text_hash="h", category_id=None)
        s.add(lead)
        await s.commit()
        s.add(Delivery(lead_id=lead.id, user_id=40, deliver_after=now()))
        await s.commit()
    slow = tgbot.send_message

    async def slow_send(chat_id, text, **kw):
        await asyncio.sleep(0.05)
        return await slow(chat_id, text, **kw)

    monkeypatch.setattr(tgbot, "send_message", slow_send)
    await asyncio.gather(notify.flush_due(), notify.flush_due())
    assert len(tgbot.texts(40)) == 1

    async with maker() as s:
        s.add(Delivery(lead_id=lead.id, user_id=ADMIN_ID, deliver_after=now()))
        await s.commit()

    async def flood(chat_id, text, **kw):
        raise TelegramRetryAfter(method=SendMessage(chat_id=chat_id, text=text), message="flood", retry_after=5)

    monkeypatch.setattr(tgbot, "send_message", flood)
    await notify.flush_due()
    async with maker() as s:
        assert not (await s.scalar(select(Delivery).where(Delivery.user_id == ADMIN_ID))).sent  # пошлём позже


async def test_paused_user_gets_nothing_from_queue(maker, tgbot):
    await make_user(maker, 41, paused=True)
    async with maker() as s:
        lead = Lead(group_id=None, group_title="Г", author_id=556, text="Нужен сайт", text_hash="h", category_id=None)
        s.add(lead)
        await s.commit()
        s.add(Delivery(lead_id=lead.id, user_id=41, deliver_after=now()))
        await s.commit()
    await notify.flush_due()
    assert not tgbot.texts(41)


async def test_pass_to_rejects_wildcards(maker, tgbot):
    await make_user(maker, 42, username="kolya")
    await make_user(maker, 43, username="petya")
    async with maker() as s:
        lead = Lead(group_id=None, group_title="Г", author_id=557, text="Нужен сайт", text_hash="h", category_id=None)
        s.add(lead)
        await s.commit()
        d = Delivery(lead_id=lead.id, user_id=42, deliver_after=now(), sent=True)
        s.add(d)
        await s.commit()
        m = MagicMock()
        m.from_user.id = 42
        m.answer = AsyncMock()
        m.bot.me = AsyncMock(return_value=MagicMock(username="bot"))
        await leads.pass_to(s, m, {"did": d.id}, "%")
    async with maker() as s:
        assert not await s.scalar(select(Delivery).where(Delivery.user_id == 43))  # «%» не значит «кто угодно»


async def test_bot_lists_by_status_and_restore_button(maker, tgbot):
    await make_user(maker, 44)
    async with maker() as s:
        lead = Lead(group_id=None, group_title="Г", author_id=558, text="Нужен сайт", text_hash="h", category_id=None)
        s.add(lead)
        await s.commit()
        d = Delivery(lead_id=lead.id, user_id=44, deliver_after=now(), sent=True, status="rejected")
        s.add(d)
        await s.commit()
        user = await s.get(User, 44)
    c = MagicMock()
    c.data, c.from_user.id = "ll:rejected:0", 44
    c.answer, c.message.answer = AsyncMock(), AsyncMock()
    await leads.leads_list(c, user)
    markup = c.answer.call_args.kwargs["reply_markup"]  # одна заявка с навигацией
    assert any(b.callback_data == f"lead:restore:{d.id}" for row in markup.inline_keyboard for b in row)
    c.data = f"lead:restore:{d.id}"
    c.message.edit_reply_markup = AsyncMock()
    await leads.lead_action(c, user)
    async with maker() as s:
        assert (await s.get(Delivery, d.id)).status == "new"


async def test_blacklist_remove_clears_votes_and_crm_delete(maker, tgbot):
    from app.bot.handlers import blacklist
    from app.models import SpamVote
    from app.services import crm

    async with maker() as s:
        s.add(BlacklistEntry(user_id=900, username="spamer", status="spam", reason="голосов"))
        s.add_all([SpamVote(author_id=900, user_id=uid) for uid in (1, 2, 3)])
        await s.commit()
    c = MagicMock()
    c.data, c.from_user.id = "bl:rm:900", ADMIN_ID
    c.answer, c.message.edit_text, c.message.answer = AsyncMock(), AsyncMock(), AsyncMock()
    c.model_copy.side_effect = lambda update: (setattr(c, "data", update["data"]), c)[1]  # как у настоящего нажатия
    await blacklist.remove(c)
    async with maker() as s:
        assert await s.get(BlacklistEntry, 900) is None
        assert not (await s.scalars(select(SpamVote).where(SpamVote.author_id == 900))).all()
        cl = await crm.create_client(s, 45, phone="+7900")
        assert crm.display_name(cl) == "+7900"  # не «None»
        await crm.add_reminder(s, cl, 1)
        assert await crm.cancel_reminders(s, cl) == 1
        await crm.delete_client(s, cl)
        assert await s.get(Client, cl.id) is None


async def test_leads_one_by_one_with_niches_and_nav(maker, tgbot):
    """Владелец 25.10: «📥 Заявки» — выбрать список и нишу, заявки по одной с «◀️ / ▶️»; после «Не моё» навигация остаётся."""
    from app.models import Category

    await make_user(maker, 47, niches=("Сайты", "Дизайн"))
    async with maker() as s:
        cats = {c.name: c.id for c in (await s.scalars(select(Category).where(Category.name.in_(["Сайты", "Дизайн"])))).all()}
        ids = []
        for i, name in enumerate(["Сайты", "Сайты", "Дизайн"]):
            lead = Lead(author_id=600 + i, text=f"Нужен {name} {i}", text_hash=f"ob{i}", category_id=cats[name])
            s.add(lead)
            await s.flush()
            d = Delivery(lead_id=lead.id, user_id=47, deliver_after=now(), sent=True, status="new")
            s.add(d)
            await s.flush()
            ids.append(d.id)
        await s.commit()
        user = await s.get(User, 47)

    def cb_(data):
        c = MagicMock()
        c.data, c.from_user.id = data, 47
        c.answer = AsyncMock()
        return c

    c = cb_("ll:new:0")
    await leads.leads_list(c, user)
    text, markup = c.answer.call_args.args[0], c.answer.call_args.kwargs["reply_markup"]
    data = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert "Какую нишу" in text and f"lp:new:{cats['Сайты']}" in data and "lp:new:0" in data
    c = cb_(f"lv:new:{cats['Сайты']}:0:0:c")
    await leads.leads_view(c, user)
    text, markup = c.answer.call_args.args[0], c.answer.call_args.kwargs["reply_markup"]
    data = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert "1 из 2" in text and "Сайты 1" in text and f"lv:new:{cats['Сайты']}:0:{ids[1]}:n" in data
    assert f"lead:reject:{ids[1]}" in data
    # «Не моё» в просмотре: кнопки сменились, а «Следующая ▶️» осталась
    c2 = cb_(f"lead:reject:{ids[1]}")
    c2.message.reply_markup = markup
    c2.message.edit_reply_markup = AsyncMock()
    await leads.lead_action(c2, user)
    new = c2.message.edit_reply_markup.call_args.kwargs["reply_markup"]
    assert f"lv:new:{cats['Сайты']}:0:{ids[1]}:n" in [b.callback_data for row in new.inline_keyboard for b in row]
    c = cb_(f"lv:new:{cats['Сайты']}:0:{ids[1]}:n")
    await leads.leads_view(c, user)
    text = c.answer.call_args.args[0]
    assert "Сайты 0" in text and "1 из 1" in text  # убранная из «Новых» не мешает листать


async def test_leads_by_place_home_region_first(maker, tgbot):
    """Владелец 25.10: в «📥 Заявки» выбрать, где смотреть; свой регион (Дагестан) — первым."""
    from app.services import geo

    await make_user(maker, 48)
    async with maker() as s:
        for i, (city, region) in enumerate([("Москва", "Москва"), ("Махачкала", "Дагестан"), ("Дербент", "Дагестан"),
                                            ("Москва", "Москва"), ("Москва", "Москва")]):
            lead = Lead(author_id=700 + i, text=f"Нужен сайт {i}", text_hash=f"pl{i}", city=city, region=region)
            s.add(lead)
            await s.flush()
            s.add(Delivery(lead_id=lead.id, user_id=48, deliver_after=now(), sent=True, status="new"))
        await s.commit()
        user = await s.get(User, 48)
    c = MagicMock()
    c.data, c.from_user.id, c.answer = "lp:new:0", 48, AsyncMock()
    await leads.place_menu(c, user)
    markup = c.answer.call_args.kwargs["reply_markup"]
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert labels[0].startswith("🌍 Все места · 5") and labels[1] == "📍 Дагестан · 2"  # Москвы больше, но свой регион первым
    dag = geo.KNOWN_PLACES.index("Дагестан") + 1
    c.data = f"lv:new:0:{dag}:0:c"
    await leads.leads_view(c, user)
    text = c.answer.call_args.args[0]
    assert "📍 Дагестан" in text and "из 2" in text


async def test_admin_user_card_profile_buttons(maker, monkeypatch):
    from app.bot.handlers import users_admin

    await make_user(maker, 4242, username="ali_master")
    await make_user(maker, 4343)
    sent = {}

    async def fake_reply(target, text, markup=None, **kw):
        sent["text"], sent["markup"] = text, markup

    monkeypatch.setattr(users_admin, "reply", fake_reply)

    def urls():
        return [b.url for row in sent["markup"].inline_keyboard for b in row if b.url]

    await users_admin.user_card(None, 4242)
    assert "tg://user?id=4242" in urls() and "https://t.me/ali_master" in urls()
    await users_admin.user_card(None, 4343)
    assert urls() == ["tg://user?id=4343"]
