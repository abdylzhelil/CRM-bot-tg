"""/deleteme, «сторож», воронка, обучающее сообщение."""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy import select

from app.bot.handlers.stats import funnel_text
from app.bot.setup import delete_yes, deleteme
from app.models import Client, Delivery, Group, Payment, User, UserCategory, now
from app.services import erase, heartbeat, intake, referrals
from app.services.antispam import AuthorInfo
from app.services.pipeline import Incoming
from tests.conftest import ADMIN_ID, make_user


async def test_erase_user_removes_everything_personal(maker, tgbot):
    await make_user(maker, 60, referred_by=0)
    await make_user(maker, 61, referred_by=60)
    async with maker() as s:
        s.add(Client(user_id=60, name="Клиент"))
        s.add(Group(tg_id=-800, username="g", title="Группа", status="joined", catalog=True, submitted_by=60,
                    ad_until=now() + timedelta(days=3)))
        s.add(Payment(user_id=60, days=30, amount=49000, currency="RUB", charge_id="x"))
        await s.commit()
    lead = await intake.handle(Incoming(text="Ищу кто сделает лендинг для магазина", author=AuthorInfo(
        user_id=999, username="c", first_name="А"), group_tg_id=-1, group_title="Чат", message_id=1))
    async with maker() as s:
        assert await s.scalar(select(Delivery).where(Delivery.user_id == 60))
        assert await erase.erase_user(s, 60)
    async with maker() as s:
        assert await s.get(User, 60) is None
        assert not (await s.scalars(select(Delivery).where(Delivery.user_id == 60))).all()
        assert not (await s.scalars(select(Client).where(Client.user_id == 60))).all()
        assert not (await s.scalars(select(UserCategory).where(UserCategory.user_id == 60))).all()
        g = await s.scalar(select(Group).where(Group.tg_id == -800))
        assert g.submitted_by == 0 and g.ad_until is None  # группа осталась, привязки к человеку нет
        assert (await s.get(User, 61)).referred_by == 0
        assert await s.scalar(select(Payment).where(Payment.charge_id == "x"))  # запись об оплате — для учёта
        assert not await erase.erase_user(s, ADMIN_ID)  # администратора удалить нельзя


async def test_deleteme_command_confirmation(maker, tgbot):
    m = MagicMock()
    m.answer = AsyncMock()
    await deleteme(m, SimpleNamespace(tg_id=5, is_admin=False, plan="full", plan_until=now() + timedelta(days=5)))
    text = m.answer.call_args.args[0]
    assert "Удалить мои данные" in text and "без возврата" in text  # оплаченный тариф — отдельное предупреждение
    await make_user(maker, 62)
    c = MagicMock()
    c.from_user.id = 62
    c.answer, c.message.edit_text = AsyncMock(), AsyncMock()
    await delete_yes(c)
    async with maker() as s:
        assert await s.get(User, 62) is None
    assert "удалены" in c.message.edit_text.call_args.args[0]


async def test_heartbeat_ping_and_off_by_default(monkeypatch):
    from aiohttp import web
    from aiohttp.test_utils import TestServer

    hits = []

    async def ok(request):
        hits.append(request.path)
        return web.Response(text="OK")

    app = web.Application()
    app.router.add_get("/ping/abc", ok)
    server = TestServer(app)
    await server.start_server()
    try:
        assert await heartbeat.ping(str(server.make_url("/ping/abc")))
        assert not await heartbeat.ping(str(server.make_url("/нет")))  # 404 — не засчитываем
    finally:
        await server.close()
    assert hits == ["/ping/abc"]
    from app import config

    monkeypatch.setattr(config, "HEARTBEAT_URL", "")
    await heartbeat.heartbeat_loop()  # пустой адрес — цикл сразу завершается


async def test_funnel_counts(maker, tgbot):
    await make_user(maker, 70)
    async with maker() as s:
        s.add(User(tg_id=71, verified=True, onboarded=False))
        s.add(User(tg_id=72, verified=False, onboarded=False))
        s.add(Payment(user_id=70, days=30, amount=49000, currency="RUB", charge_id="f1"))
        await s.commit()
    text = await funnel_text()
    assert "Зашли: 3" in text and "прошли проверку: 2" in text and "завершили регистрацию: 1" in text and "оплатили: 1" in text


def test_source_tag_parsing():
    assert referrals.parse_source("src_site") == "site"
    assert referrals.parse_source("src_Site_2") == "site_2"
    assert referrals.parse_source("src_") == "" and referrals.parse_source("ref_5") == "" and referrals.parse_source(None) == ""
    assert referrals.parse_source("src_" + "a" * 25) == "" and referrals.parse_source("src_ёж") == "" and referrals.parse_source("src_a-b") == ""


async def test_source_saved_once_and_shown_in_funnel(maker, tgbot):
    async with maker() as s:
        s.add(User(tg_id=81, verified=True, onboarded=False))
        s.add(User(tg_id=82, verified=True, onboarded=False))
        await s.commit()
    for uid, payload in ((81, "src_site"), (81, "src_other"), (82, "src_site"), (82, None)):
        async with maker() as s:
            await referrals.attach_source(s, await s.get(User, uid), payload)
    async with maker() as s:
        assert (await s.get(User, 81)).source == "site"  # второй заход источник не меняет
    await make_user(maker, 83, source="site")  # уже зарегистрированный: считается как «зарегистрировался»
    text = await funnel_text()
    assert "site 3/1" in text
    async with maker() as s:  # зарегистрированному метку уже не ставим
        await referrals.attach_source(s, await s.get(User, 83), "src_new")
        assert (await s.get(User, 83)).source == "site"
