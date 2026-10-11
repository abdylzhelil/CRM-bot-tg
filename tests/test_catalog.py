"""Этап 2: пользователи добавляют группы и каналы, каталог, бонусы, открытые каналы."""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy import select

from app.bot.handlers import catalog as ch
from app.bot.handlers import submissions
from app.models import Category, FeedChat, Group, User, now
from app.scout import channels
from app.services import catalog, geo, intake
from app.services.antispam import AuthorInfo
from app.services.pipeline import Incoming
from tests.conftest import ADMIN_ID, make_user


def cb(data, user_id=50):
    c = MagicMock()
    c.data = data
    c.from_user.id = user_id
    c.answer, c.message.edit_text, c.message.answer = AsyncMock(), AsyncMock(), AsyncMock()
    c.message.edit_reply_markup = AsyncMock()
    c.bot.send_message = AsyncMock()
    return c


def text_msg(user_id=50):
    m = MagicMock()
    m.from_user.id = user_id
    m.answer = AsyncMock()
    return m


def chat(tg_id, username, kind="supergroup", title="Чат"):
    return SimpleNamespace(id=tg_id, username=username, type=kind, title=title)


def test_parse_username():
    ok = ["t.me/my_group", "https://t.me/my_group/15", "@my_group", "my_group", "https://t.me/s/my_group", "t.me/my_group?start=1"]
    assert all(catalog.parse_username(x) == "my_group" for x in ok)
    for bad in ["https://t.me/+AbCdEf123", "t.me/joinchat/AAA", "t.me/c/123/4", "ab", "", "привет"]:
        assert catalog.parse_username(bad) is None


async def category_id(maker, name="Сайты"):
    async with maker() as s:
        return await s.scalar(select(Category.id).where(Category.name == name, Category.parent_id.is_(None)))


async def submit_group(maker, tgbot, user_id=50, tg_id=-1001, username="web_chat", kind="supergroup", city_idx="0"):
    tgbot.chats = {f"@{username}": chat(tg_id, username, kind)}
    m = text_msg(user_id)
    m.bot = tgbot
    async with maker() as s:
        await ch.grp_link(s, m, {}, f"t.me/{username}")
    async with maker() as s:
        g = await s.scalar(select(Group).where(Group.tg_id == tg_id))
    if g is None or g.status != "draft":
        return g
    await ch.pick_category(cb(f"cg:cat:{g.id}:{await category_id(maker)}", user_id), await _user(maker, user_id))
    await ch.pick_city(cb(f"cg:city:{g.id}:{city_idx}", user_id), AsyncMock(), await _user(maker, user_id))
    async with maker() as s:
        return await s.get(Group, g.id)


async def _user(maker, user_id):
    async with maker() as s:
        return await s.get(User, user_id)


async def test_submit_approve_catalog(maker, tgbot):
    await make_user(maker, 50, city="Махачкала")
    g = await submit_group(maker, tgbot)
    assert g.status == "pending" and g.submitted_by == 50 and g.city == "Махачкала" and g.kind == "group"
    assert any("Новая группа" in t for t in tgbot.texts(ADMIN_ID))  # администратору ушла карточка

    c = cb(f"up:ok:{g.id}", ADMIN_ID)
    await submissions.decide(c)
    async with maker() as s:
        g = await s.get(Group, g.id)
        assert g.catalog and g.status == "approved"  # теперь её подхватит разведчик
        user = await s.get(User, 50)
        mine = await catalog.browse(s, user, {g.category_id}, everything=False)
        other = await catalog.browse(s, User(tg_id=77, city="Москва"), {g.category_id}, everything=False)
    assert [x.id for x in mine] == [g.id]
    assert other == []  # группа Махачкалы не показывается москвичу
    c.bot.send_message.assert_awaited()  # автору сообщили


async def test_reject_and_duplicates_and_limits(maker, tgbot):
    await make_user(maker, 50)
    g = await submit_group(maker, tgbot)
    await submissions.decide(cb(f"up:no:{g.id}", ADMIN_ID))
    async with maker() as s:
        assert (await s.get(Group, g.id)).status == "rejected"
    # повторно ту же группу уже нельзя — она в базе
    tgbot.chats = {"@web_chat": chat(-1001, "web_chat")}
    m = text_msg()
    m.bot = tgbot
    async with maker() as s:
        await ch.grp_link(s, m, {}, "@web_chat")
    assert "уже есть" in m.answer.call_args.args[0]
    # суточный лимит: 3 группы в сутки
    for i in range(3):
        await submit_group(maker, tgbot, tg_id=-2000 - i, username=f"chat_numb{i}")
    blocked = await submit_group(maker, tgbot, tg_id=-3000, username="chat_extra")
    assert blocked is None


async def test_private_and_tiny_rejected(maker, tgbot):
    await make_user(maker, 50)
    tgbot.chats = {"@some_user": chat(5, "some_user", "private")}
    m = text_msg()
    m.bot = tgbot
    async with maker() as s:
        await ch.grp_link(s, m, {}, "@some_user")
        await ch.grp_link(s, m, {}, "@not_exists")
        await ch.grp_link(s, m, {}, "t.me/+SecretInvite")
    texts = [c.args[0] for c in m.answer.call_args_list]
    assert "не группа" in texts[0] and "Не нашёл" in texts[1] and "открытую" in texts[2]


async def test_bonus_days_for_leads_from_user_group(maker, tgbot, monkeypatch):
    await make_user(maker, 50)
    g = await submit_group(maker, tgbot, tg_id=-1005, username="bonus_chat")
    await submissions.decide(cb(f"up:ok:{g.id}", ADMIN_ID))
    uid = iter(range(900, 999))
    for i in range(5):
        a = AuthorInfo(user_id=next(uid), username="c", first_name="Али")
        await intake.handle(Incoming(text=f"Ищу кто сделает лендинг для магазина номер {i}", author=a,
                                     group_tg_id=-1005, group_title="Чат", message_id=i))
    async with maker() as s:
        u = await s.get(User, 50)
        g = await s.get(Group, g.id)
    assert g.leads_count == 5 and g.bonus_rewarded == 5
    assert u.plan == "full" and now() + timedelta(hours=23) < u.plan_until < now() + timedelta(days=1, hours=1)
    assert any("+1 дн" in t for t in tgbot.texts(50))


async def test_approve_covered_by_moderation_bot_and_channel(maker, tgbot):
    await make_user(maker, 50)
    async with maker() as s:
        s.add(FeedChat(tg_id=-1001, title="x"))
        await s.commit()
    g = await submit_group(maker, tgbot, kind="supergroup")
    async with maker() as s:
        await catalog.approve(s, await s.get(Group, g.id))
        assert (await s.get(Group, g.id)).status == "covered"
    g2 = Group(tg_id=-1009, username="ads_channel", title="Канал", kind="channel", status="pending", submitted_by=50)
    async with maker() as s:
        s.add(g2)
        await s.commit()
        await catalog.approve(s, g2)
        assert g2.status == "joined" and g2.catalog  # каналы не требуют вступления


PAGE = """
<div class="tgme_widget_message_wrap js-widget_message_wrap"><div class="tgme_widget_message" data-post="ads_channel/10">
<div class="tgme_widget_message_text js-message_text" dir="auto">Старое объявление, пишите @old_man</div></div></div>
<div class="tgme_widget_message_wrap js-widget_message_wrap"><div class="tgme_widget_message" data-post="ads_channel/11">
<div class="tgme_widget_message_text js-message_text" dir="auto">Ищу, кто сделает лендинг для магазина,<br/>пишите <a href="https://t.me/zakazchik_1">@zakazchik_1</a> &amp; звоните</div></div></div>
<div class="tgme_widget_message_wrap js-widget_message_wrap"><div class="tgme_widget_message" data-post="ads_channel/12">
<div class="tgme_widget_message_text js-message_text" dir="auto">Ищу кто сделает лендинг, контактов нет</div></div></div>
"""


def test_parse_channel_page():
    posts = channels.parse_posts(PAGE)
    assert [p for p, _ in posts] == [10, 11, 12]
    assert "лендинг для магазина,\nпишите @zakazchik_1 & звоните" in posts[1][1]
    assert channels.contact_of(posts[1][1], "ads_channel") == "zakazchik_1"
    assert channels.contact_of("пишите @ads_channel или @my_bot", "ads_channel") == ""
    assert channels.author_for("Zakazchik_1").user_id == channels.author_for("zakazchik_1").user_id < 0


async def test_channel_polling_makes_leads(maker, tgbot):
    async with maker() as s:
        s.add(Group(tg_id=-1009, username="ads_channel", title="Канал", kind="channel", status="joined", catalog=True))
        await s.commit()
        g = await s.scalar(select(Group))
    posts = channels.parse_posts(PAGE)
    assert await channels.poll_channel(g, posts) == 0  # первый опрос только запоминает место
    async with maker() as s:
        g = await s.scalar(select(Group))
        assert g.last_post_id == 12
    new = posts + [(13, "Нужен сайт-визитка для салона, пишите @salon_owner")]
    assert await channels.poll_channel(g, new) == 1
    async with maker() as s:
        lead = (await s.execute(select(__import__("app.models", fromlist=["Lead"]).Lead))).scalar_one()
    assert lead.username == "salon_owner" and lead.link == "https://t.me/ads_channel/13"
    assert any("salon_owner" in t for t in tgbot.texts(ADMIN_ID))
