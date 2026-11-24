"""Придуманные сообщения: реальные клиентские тексты не публикуются."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import func, select

from app.models import Category, Delivery, Lead, User, now
from app.services import distribute, geo, intake, lead_sources, rules, settings, upsell
from app.services.text import match_tasks, match, compile_negatives
from tests.conftest import ADMIN_ID, make_user
from tests.test_multiuser import msg
from tests.test_service_requests import default_rules
from tests.test_webapp import client, tma


@pytest.mark.parametrize("text,expected", [
    ("Нужно перевезти шкаф из Махачкалы в Москву", ("Москва", "Москва")),
    ("Нужно перевезти шкаф из Москвы в Махачкалу", ("Махачкала", "Дагестан")),
    ("Нужен мастер не в Москве, а в Каспийске", ("Каспийск", "Дагестан")),
    ("Нужен мастер, ответить до 18:00 по МСК", ("Махачкала", "Дагестан")),
    ("Нужен мастер, связь по времени Москвы", ("Махачкала", "Дагестан")),
    ("В Дубае нужен сантехник", ("Дубай", "ОАЭ")),
    ("В Алматы нужен электрик", ("Алматы", "Казахстан")),
    ("Ищу мастера в Каспийске или Дербенте", ("", "Дагестан")),
    ("Ищу мастера в Москве или Каспийске", ("", "")),
])
def test_request_place(text, expected):
    assert geo.detect(text, "Объявления Махачкала") == expected


def test_group_location_is_marked_as_inferred():
    assert "по группе" in geo.place_note("Нужен сантехник", "Махачкала", "Махачкала", "Дагестан")
    assert "в сообщении" in geo.place_note("В Каспийске нужен сантехник", "Махачкала", "Каспийск", "Дагестан")
    assert geo.detect("Нужен сантехник", "Московские дагестанцы") == ("Москва", "Москва")
    assert not geo.matches("Дагестан", False, "Дубай", "ОАЭ")
    assert not geo.matches("", False, "Дубай", "ОАЭ")
    assert "отличается" in geo.place_note("Нужен сантехник в Каспийске", "Махачкала", "Москва", "Москва")


@pytest.mark.parametrize("text,expected", [
    ("Нужен сантехник и грузчики", {"Ремонт и строительство", "Перевозки и доставка"}),
    ("Нужно собрать шкаф и перенести диван", {"Мебель и интерьер", "Перевозки и доставка"}),
    ("В Махачкале нужен сантехник. В Каспийске нужны грузчики", {"Ремонт и строительство", "Перевозки и доставка"}),
    ("Нужен сайт для доставки еды", {"Сайты"}),
    ("Нужен сантехник. Грузчики уже пришли", {"Ремонт и строительство"}),
    ("Нужен сантехник и не нужны грузчики", {"Ремонт и строительство"}),
    ("Нужен сантехник и грузчики уже есть", {"Ремонт и строительство"}),
    ("Ищу мастера собрать шкаф, с меня обед", {"Мебель и интерьер"}),
])
def test_independent_service_requests(text, expected):
    found = match_tasks(text, default_rules(), context="Махачкала")
    assert {t.match.category_name.split(" → ")[0] for t in found} == expected


def test_category_city_cannot_be_bypassed_by_group_or_another_task():
    r = default_rules()
    assert match("Ищу помещение в Каспийске", r, context="Махачкала") is None
    assert match("Ищу помещение в Дагестане", r, context="Дербент") is None
    found = match_tasks("В Махачкале нужен сантехник. В Каспийске сниму помещение", r, context="Махачкала")
    assert {t.match.category_name for t in found} == {"Ремонт и строительство"}
    assert geo.detect(found[0].place_text) == ("Махачкала", "Дагестан")


def test_route_does_not_take_the_city_of_another_sentence():
    text = "Нужно перевезти диван из Махачкалы в Москву. В Каспийске нужен сантехник"
    assert geo.route_places(text) == [("Махачкала", "Дагестан"), ("Москва", "Москва")]
    assert geo.matches_request("Махачкала", False, "Москва", "Москва", text, route=True)
    assert not geo.matches_request("Каспийск", False, "Москва", "Москва", text, route=True)


def test_splitting_does_not_bypass_category_negative():
    r = default_rules()
    next(n for n in r.nodes if n.name == "Перевозки и доставка").negatives = compile_negatives(["диван"])
    found = match_tasks("Нужно собрать диван и перенести коробки", r)
    assert {t.match.category_name for t in found} == {"Мебель и интерьер"}


async def test_multi_niche_delivery_once_and_filters(maker, tgbot):
    async with maker() as s:
        await settings.put(s, "share_gap_min", "0")
        await s.commit()
    niches = ("Мебель и интерьер", "Перевозки и доставка")
    await make_user(maker, 610, niches=(niches[0],), plan="full")
    await make_user(maker, 611, niches=(niches[1],), plan="full")
    await make_user(maker, 612, niches=niches, plan="full")
    await make_user(maker, 613, niches=("Дизайн",), plan="full")
    lead = await intake.handle(msg("Нужно собрать шкаф и перенести диван в Махачкале"))
    async with maker() as s:
        tasks = await lead_sources.tasks(s, lead)
        assert len(tasks) == 2
        rows = (await s.scalars(select(Delivery).where(Delivery.lead_id.in_([t.id for t in tasks])))).all()
        ids = [d.user_id for d in rows]
        assert set(ids) == {ADMIN_ID, 610, 611, 612} and len(ids) == len(set(ids))
        cats = {c.name: c.id for c in (await s.scalars(select(Category))).all()}
        from app.bot.handlers import leads
        facets = await leads._niches(s, 612, "new")
        assert {cid for cid, _, n in facets if n == 1} == {cats[n] for n in niches}
        for name in niches:
            assert await s.scalar(select(func.count()).select_from(Delivery).join(Lead).where(
                Delivery.user_id == 612, lead_sources.in_niches([cats[name]]))) == 1
        assert await distribute.preview(s, await s.get(User, 612)) == []
        assert await upsell.week_stats(s, await s.get(User, 612)) == (1, 1)
        assert await distribute.distribute(s, tasks[1]) == []
        mine = next(d for d in rows if d.user_id == 610)
        forwarded = MagicMock()
        forwarded.from_user.id = 610
        forwarded.answer = AsyncMock()
        await leads.pass_to(s, forwarded, {"did": mine.id}, "611")
        assert "уже есть" in forwarded.answer.call_args.args[0]
        assert await s.scalar(select(func.count()).select_from(Delivery).where(Delivery.user_id == 611)) == 1
    assert len(tgbot.texts(612)) == 1
    assert "Мебель" in tgbot.texts(612)[0] and "Перевозки" in tgbot.texts(612)[0]


async def test_offer_feedback_covers_all_tasks(maker, tgbot):
    from app.services import lead_actions
    from app.models import LabeledText

    await make_user(maker, 670, niches=("Мебель и интерьер",), plan="full")
    await make_user(maker, 671, niches=("Перевозки и доставка",), plan="full")
    await make_user(maker, 672, niches=("Перевозки и доставка",))
    async with maker() as s:
        await settings.put(s, "offer_votes_to_learn", "2")
        await s.commit()
    lead = await intake.handle(msg("Нужно собрать новый письменный стол и перенести тяжелый диван в Махачкале завтра вечером"))
    async with maker() as s:
        for uid in (670, 671):
            d = await lead_sources.delivery(s, uid, lead)
            await lead_actions.vote_offer(s, await s.get(Lead, d.lead_id), await s.get(User, uid), d)
        pending = await lead_sources.delivery(s, 672, lead)
        assert pending.sent and pending.status == "rejected"
        assert await s.scalar(select(func.count()).select_from(LabeledText).where(LabeledText.kind == "offer")) == 1


async def test_task_cities_stay_with_their_services(maker, tgbot):
    await make_user(maker, 620, niches=("Ремонт и строительство",), city="Махачкала", plan="full")
    await make_user(maker, 621, niches=("Перевозки и доставка",), city="Каспийск", plan="full")
    await make_user(maker, 622, niches=("Перевозки и доставка",), city="Махачкала", plan="full")
    lead = await intake.handle(msg("В Махачкале нужен сантехник. В Каспийске нужны грузчики"))
    async with maker() as s:
        tasks = await lead_sources.tasks(s, lead)
        cities = {await distribute.root_category(s, t.category_id): t.city for t in tasks}
        assert set(cities.values()) == {"Махачкала", "Каспийск"}
        ids = set((await s.scalars(select(Delivery.user_id).where(Delivery.lead_id.in_([t.id for t in tasks])))).all())
        assert ids == {ADMIN_ID, 620, 621}
        transport = next(t for t in tasks if t.city == "Каспийск")
        assert not await s.scalar(select(Lead.id).where(Lead.id == lead.id,
            lead_sources.filter_tasks([transport.category_id], "Махачкала")))


async def test_shared_recipient_cap_and_concurrent_duplicates(maker, tgbot):
    async with maker() as s:
        await settings.put(s, "max_recipients", "2")
        await s.commit()
    for uid in range(630, 636):
        await make_user(maker, uid, niches=("Мебель и интерьер", "Перевозки и доставка"), plan="full")
    message = msg("Нужно собрать шкаф и перенести диван", author=999001)
    a, b = await asyncio.gather(intake.handle(message), intake.handle(message))
    lead = a or b
    assert bool(a) != bool(b)
    async with maker() as s:
        tasks = await lead_sources.tasks(s, lead)
        assert len(tasks) == 2
        users = (await s.scalars(select(Delivery.user_id).where(Delivery.lead_id.in_([t.id for t in tasks])))).all()
        assert len(set(users) - {ADMIN_ID}) == 2
        assert len(users) == len(set(users))


async def test_preview_and_free_limit_count_source_once(maker, tgbot):
    lead = await intake.handle(msg("Нужно собрать шкаф и перенести диван"))
    await make_user(maker, 640, niches=("Мебель и интерьер", "Перевозки и доставка"))
    async with maker() as s:
        user = await s.get(User, 640)
        shown = await distribute.preview(s, user)
        assert len(shown) == 1
        assert await distribute.preview(s, user) == []
        assert await distribute.delivered_today(s, 640) == 0
        assert await upsell.week_stats(s, user) == (1, 1)
        await settings.put(s, "free_daily_limit", "1")
        await s.commit()
    fresh = await intake.handle(msg("Нужна сборка письменного стола и разгрузка кирпича"))
    async with maker() as s:
        assert await distribute.delivered_today(s, 640) == 1
        assert await upsell.week_stats(s, await s.get(User, 640)) == (2, 2)


async def test_transport_reaches_both_route_ends(maker, tgbot):
    for uid, city in ((650, "Махачкала"), (651, "Москва"), (652, "Каспийск")):
        await make_user(maker, uid, niches=("Перевозки и доставка",), city=city, plan="full")
    lead = await intake.handle(msg("Нужно перевезти диван из Махачкалы в Москву"))
    async with maker() as s:
        users = set((await s.scalars(select(Delivery.user_id).where(Delivery.lead_id == lead.id))).all())
        assert users == {ADMIN_ID, 650, 651}
        assert await upsell.week_stats(s, await s.get(User, 650)) == (1, 1)


async def test_web_filters_keep_service_and_city_together(client, maker, tgbot):
    async with maker() as s:
        await settings.put(s, "share_gap_min", "0")
        await s.commit()
    await make_user(maker, 660, niches=("Ремонт и строительство", "Перевозки и доставка"), plan="full")
    await intake.handle(msg("В Махачкале нужен сантехник. В Каспийске нужны грузчики"))
    async with maker() as s:
        cats = {c.name: c.id for c in (await s.scalars(select(Category))).all()}
    repair, transport = cats["Ремонт и строительство"], cats["Перевозки и доставка"]
    for niche, place, count in ((repair, "Махачкала", 1), (transport, "Каспийск", 1),
                                (repair, "Каспийск", 0), (transport, "Махачкала", 0)):
        response = await client.get("/api/leads", params={"cat": niche, "place": place}, headers=tma(660))
        assert response.status == 200
        body = await response.json()
        assert len(body["items"]) == count
        if count:
            assert "place_note" in body["items"][0]
