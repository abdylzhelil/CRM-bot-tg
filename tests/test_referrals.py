"""Этап 3: приглашения (+дни обоим, % от бонусов) и передача заявки коллеге."""
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy import select

from app.bot.handlers import leads
from app.models import Delivery, Group, User, now
from app.services import catalog, intake, referrals
from app.services.antispam import AuthorInfo
from app.services.pipeline import Incoming
from tests.conftest import ADMIN_ID, make_user

_ids = iter(range(5000, 90000))


def msg_obj(user_id):
    m = MagicMock()
    m.from_user.id = user_id
    m.answer = AsyncMock()
    m.bot.me = AsyncMock(return_value=MagicMock(username="crm_bot"))
    return m


def lead_msg(text="Ищу кто сделает лендинг для магазина", group=-100500):
    return Incoming(text=text, author=AuthorInfo(user_id=next(_ids), username="cl", first_name="Али"),
                    group_tg_id=group, group_title="Чат", message_id=next(_ids))


def test_payload():
    assert referrals.parse_payload("ref_42") == 42
    assert referrals.parse_payload("ref_x") is None and referrals.parse_payload(None) is None
    assert referrals.link("bot", 7) == "https://t.me/bot?start=ref_7"


async def new_user(maker, tg_id, onboarded=False):
    async with maker() as s:
        s.add(User(tg_id=tg_id, name=f"u{tg_id}", onboarded=onboarded))
        await s.commit()
        return await s.get(User, tg_id)


async def test_signup_reward_once_both_sides(maker, tgbot):
    await make_user(maker, 10)
    friend = await new_user(maker, 11)
    async with maker() as s:
        assert await referrals.attach(s, friend, 10)
        assert not await referrals.attach(s, friend, 10)  # второй раз — нет
        assert await referrals.reward_signup(s, friend) is None  # рано: настройка не завершена
    async with maker() as s:
        (await s.get(User, 11)).onboarded = True
        await s.commit()
    async with maker() as s:
        assert await referrals.reward_signup(s, friend) == 10
        assert await referrals.reward_signup(s, friend) is None  # награда один раз
    async with maker() as s:
        for uid in (10, 11):
            u = await s.get(User, uid)
            assert u.plan == "full" and timedelta(days=6, hours=23) < u.plan_until - now() < timedelta(days=7, hours=1)
        assert await referrals.stats(s, 10) == (1, 1)


async def test_no_self_or_existing_user_referral(maker):
    u = await make_user(maker, 20)  # уже зарегистрирован
    async with maker() as s:
        assert not await referrals.attach(s, u, 99)
    fresh = await new_user(maker, 21)
    async with maker() as s:
        assert not await referrals.attach(s, fresh, 21) and not await referrals.attach(s, fresh, 12345)


async def test_percent_of_friend_bonus_goes_to_referrer(maker, tgbot):
    await make_user(maker, 30)
    await make_user(maker, 31, referred_by=30, ref_rewarded=True)
    async with maker() as s:
        s.add(Group(tg_id=-100500, username="gr", title="Чат", kind="group", status="approved",
                    catalog=True, submitted_by=31))
        await s.commit()
    for i in range(5):
        await intake.handle(lead_msg(f"Ищу кто сделает лендинг для магазина номер {i}"))
    async with maker() as s:
        friend, referrer = await s.get(User, 31), await s.get(User, 30)
    assert friend.plan == "full" and referrer.plan == "full"
    gain = referrer.plan_until - now()  # 20% от 1 дня = 4,8 часа
    assert timedelta(hours=4) < gain < timedelta(hours=5.5)
    assert any("вам +5 ч." in t for t in tgbot.texts(30))


async def test_pass_lead_to_colleague(maker, tgbot):
    await make_user(maker, 40)
    await make_user(maker, 41, niches=("Дизайн",), username="colleague")
    lead = await intake.handle(lead_msg())
    async with maker() as s:
        mine = await s.scalar(select(Delivery).where(Delivery.user_id == ADMIN_ID, Delivery.lead_id == lead.id))
        admin_did = mine.id
    m = msg_obj(ADMIN_ID)
    async with maker() as s:
        await leads.pass_to(s, m, {"did": admin_did}, "@colleague")
    async with maker() as s:
        d = await s.scalar(select(Delivery).where(Delivery.user_id == 41, Delivery.lead_id == lead.id))
    assert d is not None and d.from_user == ADMIN_ID and d.sent  # ушла сразу, без задержки бесплатного тарифа
    assert any("Передал(а)" in t for t in tgbot.texts(41))
    # повторно — уже есть
    async with maker() as s:
        await leads.pass_to(s, m, {"did": admin_did}, "41")
    assert "уже есть" in m.answer.call_args.args[0]


async def test_pass_to_stranger_gives_invite_and_limit(maker, tgbot):
    lead = await intake.handle(lead_msg())
    async with maker() as s:
        did = (await s.scalar(select(Delivery).where(Delivery.lead_id == lead.id))).id
    m = msg_obj(ADMIN_ID)
    async with maker() as s:
        await leads.pass_to(s, m, {"did": did}, "@nobody")
    assert "start=ref_1" in m.answer.call_args.args[0]
    # чужую заявку передать нельзя
    other = msg_obj(999)
    async with maker() as s:
        await leads.pass_to(s, other, {"did": did}, "@nobody")
    assert "не найдена" in other.answer.call_args.args[0]
