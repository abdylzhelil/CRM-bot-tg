"""Оплата и польза 💎 (25.10): «лимит исчерпан», цифры в тарифе, пробный 💎, гарантия заявок, заявки после оплаты."""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy import select

from app.models import Delivery, Payment, User, now
from app.services import intake, payments, settings, upsell
from tests.conftest import make_user
from tests.test_multiuser import msg


async def test_missed_after_limit_and_notice(maker, tgbot, monkeypatch):
    import app.main as main

    await make_user(maker, 40)
    for i in range(5):
        await intake.handle(msg(f"Ищу разработчика сайта номер {i} для своей компании {i * 7}"))
    for i in range(3):
        await intake.handle(msg(f"Нужен лендинг срочно, кто сделает? вариант {i}"))
    async with maker() as s:
        u = await s.get(User, 40)
    assert u.missed_day == upsell.today() and u.missed_count == 3
    assert await main.missed_once() == 1
    assert any("лимит" in t and "3" in t for t in tgbot.texts(40))
    assert await main.missed_once() == 0  # раз в день


async def test_week_stats_in_tariff(maker, tgbot):
    await make_user(maker, 41)
    for i in range(7):
        await intake.handle(msg(f"Ищу разработчика сайта номер {i} для своей компании {i * 7}"))
    async with maker() as s:
        u = await s.get(User, 41)
        total, got = await upsell.week_stats(s, u)
    assert total == 7 and got == 5


async def test_trial_once_and_ends(maker, tgbot):
    import app.main as main

    u = await make_user(maker, 42)
    async with maker() as s:
        assert await upsell.start_trial(s, u) == 3
        await s.commit()
        assert await upsell.start_trial(s, u) == 0  # второй раз — нет
        row = await s.get(User, 42)
        assert row.plan == "full" and row.on_trial
        row.plan_until = now() + timedelta(hours=10)  # последние сутки — напоминание
        await s.commit()
    await main.expiry_once()
    assert any("Пробный 💎 заканчивается" in t for t in tgbot.texts(42))
    async with maker() as s:
        row = await s.get(User, 42)
        row.plan_until = now() - timedelta(minutes=1)
        await s.commit()
    await main.expiry_once()
    async with maker() as s:
        row = await s.get(User, 42)
    assert row.plan == "free" and not row.on_trial
    assert any("Пробный 💎 закончился" in t for t in tgbot.texts(42))


async def test_trial_setting_off(maker):
    u = await make_user(maker, 43)
    async with maker() as s:
        await settings.put(s, "trial_days", "0")
        assert await upsell.start_trial(s, u) == 0


async def test_guarantee_adds_days(maker, tgbot):
    import app.main as main

    await make_user(maker, 44, plan="full", plan_until=now() + timedelta(days=20))
    async with maker() as s:
        s.add(Payment(user_id=44, days=30, amount=49000, currency="RUB", charge_id="g1"))
        await s.commit()
    await main.expiry_once()  # первая неделя начинается
    async with maker() as s:
        u = await s.get(User, 44)
        assert u.guarantee_at is not None
        u.guarantee_at = now() - timedelta(days=8)
        before = u.plan_until
        await s.commit()
    await main.expiry_once()
    async with maker() as s:
        u = await s.get(User, 44)
    assert u.plan_until - before >= timedelta(days=7) - timedelta(minutes=1)
    assert any("Добавили вам +7" in t for t in tgbot.texts(44))


async def test_guarantee_not_forever(maker, tgbot):
    """Один платёж не даёт 💎 навсегда: неделя, начатая после оплаченного срока (дни от гарантии), — без гарантии."""
    import app.main as main

    await make_user(maker, 46, plan="full", plan_until=now() + timedelta(days=10))
    async with maker() as s:
        s.add(Payment(user_id=46, days=30, amount=49000, currency="RUB", charge_id="g2", created_at=now() - timedelta(days=40)))
        await s.commit()
        u = await s.get(User, 46)
        u.guarantee_at = now() - timedelta(days=8)  # неделя началась уже после конца оплаченных 30 дней
        before = u.plan_until
        await s.commit()
    await main.expiry_once()
    async with maker() as s:
        u = await s.get(User, 46)
    assert u.plan_until == before and not any("Добавили вам" in t for t in tgbot.texts(46))


async def test_payment_sends_missed_leads(maker, tgbot):
    await make_user(maker, 45)
    for i in range(7):
        await intake.handle(msg(f"Ищу разработчика сайта номер {i} для своей компании {i * 7}"))
    async with maker() as s:
        new, _ = await payments.apply(s, 45, 30, 49000, "RUB", "pay-45")
    assert new
    assert any("прошли мимо" in t for t in tgbot.texts(45))
    async with maker() as s:
        n = len((await s.scalars(select(Delivery).where(Delivery.user_id == 45))).all())
    assert n >= 7


async def test_missed_counts_only_what_would_come(maker, tgbot, monkeypatch):
    """Честный «мимо прошло»: если заявку и с 💎 получили бы другие (делим поровну), бесплатному её не засчитываем."""
    from app.models import Category, Lead
    from app.services import distribute, settings

    async with maker() as s:
        await settings.put(s, "free_daily_limit", "0")  # все бесплатные «вне лимита»
        await settings.put(s, "max_recipients", "1")
        cat = await s.scalar(select(Category).where(Category.name == "Сайты"))
        await s.commit()
    for uid in (81, 82, 83):
        await make_user(maker, uid)
    async with maker() as s:
        lead = Lead(author_id=5, text="Нужен сайт", text_hash="mc1", category_id=cat.id)
        s.add(lead)
        await s.flush()
        await distribute.recipients(s, lead)
        await s.commit()
        counts = [(await s.get(User, uid)).missed_count for uid in (81, 82, 83)]
    assert sum(counts) == 1  # одна заявка, одно место — «мимо» засчитано только одному


async def test_payers_before_trial_users(maker, tgbot):
    from app.models import Category, Lead
    from app.services import distribute, settings

    async with maker() as s:
        await settings.put(s, "max_recipients", "1")
        cat = await s.scalar(select(Category).where(Category.name == "Сайты"))
        await s.commit()
    await make_user(maker, 91, plan="full", plan_until=now() + timedelta(days=3), on_trial=True)
    await make_user(maker, 92, plan="full", plan_until=now() + timedelta(days=30))
    async with maker() as s:
        lead = Lead(author_id=5, text="Нужен сайт", text_hash="pt1", category_id=cat.id)
        s.add(lead)
        await s.flush()
        chosen = [u.tg_id for u, _, _ in await distribute.recipients(s, lead) if not u.is_admin]
    assert chosen == [92]
