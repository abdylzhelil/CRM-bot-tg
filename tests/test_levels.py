"""Этап 4: право первооткрывателя, уровни, рейтинг по городам."""
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy import select

from app.bot.handlers import catalog as ch
from app.models import Delivery, Group, User, now
from app.services import intake, levels
from app.services.antispam import AuthorInfo
from app.services.pipeline import Incoming
from tests.conftest import ADMIN_ID, make_user

_ids = iter(range(7000, 90000))


def lead_msg(group=-100600, text=None):
    n = next(_ids)
    return Incoming(text=text or f"Ищу кто сделает лендинг для магазина номер {n}",
                    author=AuthorInfo(user_id=n, username="cl", first_name="Али"),
                    group_tg_id=group, group_title="Чат", message_id=n)


async def add_group(maker, finder, tg_id=-100600, city="", **kw):
    async with maker() as s:
        s.add(Group(tg_id=tg_id, username=f"g{abs(tg_id)}", title=f"Группа {tg_id}", kind="group", status="joined",
                    catalog=True, submitted_by=finder, city=city, **kw))
        await s.commit()


async def got(maker, lead_id):
    async with maker() as s:
        return {d.user_id: d for d in (await s.scalars(select(Delivery).where(Delivery.lead_id == lead_id))).all()}


def test_level_thresholds():
    assert levels.level_of(0, 500) == 0  # без группы в каталоге уровня нет
    assert [levels.level_of(1, n) for n in (0, 9, 10, 49, 50, 199, 200, 999, 1000)] == [1, 1, 2, 2, 3, 3, 4, 4, 5]
    assert "ещё 3" in levels.next_step(2, 47)


async def test_finder_first_paid_after_head_start_free_late(maker, tgbot):
    await make_user(maker, 50)  # бесплатный автор группы
    await make_user(maker, 51, plan="full")
    await make_user(maker, 52)
    await add_group(maker, 50)
    lead = await intake.handle(lead_msg())
    d = await got(maker, lead.id)
    assert set(d) == {ADMIN_ID, 50, 51, 52}
    t = now()
    assert d[50].deliver_after <= t and d[ADMIN_ID].deliver_after <= t  # автор и админ — сразу
    assert timedelta(minutes=1) < d[51].deliver_after - t < timedelta(minutes=3)  # платный — после форы 2 мин
    assert d[52].deliver_after - t > timedelta(minutes=20)  # бесплатный — как обычно
    assert tgbot.texts(50) and not tgbot.texts(51)  # автору пришло сразу


async def test_finder_ignores_free_daily_limit(maker, tgbot):
    await make_user(maker, 50)
    await add_group(maker, 50)
    for _ in range(7):  # лимит бесплатного — 5 в день
        lead = await intake.handle(lead_msg())
        assert 50 in await got(maker, lead.id)


async def test_no_head_start_without_finder(maker, tgbot):
    await make_user(maker, 51, plan="full")
    lead = await intake.handle(lead_msg(group=-100999))
    assert (await got(maker, lead.id))[51].deliver_after <= now()


async def test_finder_must_match_niche(maker, tgbot):
    await make_user(maker, 50, niches=("Дизайн",))
    await make_user(maker, 51, plan="full")
    await add_group(maker, 50)
    lead = await intake.handle(lead_msg())
    d = await got(maker, lead.id)
    assert 50 not in d and d[51].deliver_after <= now()  # автор не по нише — форы нет


async def test_gold_gets_longer_head_start_and_level_up_message(maker, tgbot):
    await make_user(maker, 50)
    await make_user(maker, 51, plan="full")
    await add_group(maker, 50, leads_count=198)
    async with maker() as s:
        assert await levels.head_start_min(s, 50) == 2
    await intake.handle(lead_msg())
    await intake.handle(lead_msg())  # 200 заявок -> 🥇
    async with maker() as s:
        assert (await s.get(User, 50)).level == 4 and await levels.head_start_min(s, 50) == 3
    assert any("Золото" in t for t in tgbot.texts(50))
    lead = await intake.handle(lead_msg())
    assert timedelta(minutes=2) < (await got(maker, lead.id))[51].deliver_after - now() < timedelta(minutes=4)


async def test_rating_by_city_and_screen(maker, tgbot):
    await make_user(maker, 60, city="Махачкала")
    await make_user(maker, 61, city="Москва")
    await add_group(maker, 60, tg_id=-1, city="Махачкала", leads_count=12)
    await add_group(maker, 61, tg_id=-2, city="Москва", leads_count=30)
    await add_group(maker, ADMIN_ID, tg_id=-3, city="Махачкала", leads_count=99)  # админ в рейтинге не участвует
    async with maker() as s:
        city = await levels.rating(s, "Махачкала")
        allr = await levels.rating(s)
    assert [(u.tg_id, p) for u, p in city] == [(60, 12)]
    assert [u.tg_id for u, _ in allr] == [61, 60]

    c = MagicMock()
    c.data = "cg:top:mine"
    c.answer, c.message.edit_text, c.message.answer = AsyncMock(), AsyncMock(), AsyncMock()
    async with maker() as s:
        user = await s.get(User, 60)
    await ch.top(c, user)
    text = c.answer.call_args.args[0]
    assert "Махачкала" in text and "← вы" in text and "12" in text
