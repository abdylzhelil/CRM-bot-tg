"""Этап 5: оплата тарифа через Telegram Payments и публичная статистика."""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy import func, select

from app import config
from app.bot.handlers import payments as ph
from app.models import Payment, User, now
from app.services import intake, payments, public_stats, settings
from app.services.antispam import AuthorInfo
from app.services.pipeline import Incoming
from tests.conftest import ADMIN_ID, make_user


async def set_price(maker, days, rub):
    async with maker() as s:
        await settings.put(s, f"price_{days}", str(rub))


def paid_msg(user_id, days, amount, charge="ch1"):
    m = MagicMock()
    m.answer = AsyncMock()
    m.successful_payment = SimpleNamespace(invoice_payload=f"plan:{days}", total_amount=amount, currency="RUB",
                                           telegram_payment_charge_id=charge)
    return m, SimpleNamespace(tg_id=user_id, name=f"u{user_id}")


async def test_packages_only_with_price(maker):
    await set_price(maker, 30, 990)
    async with maker() as s:
        assert await payments.packages(s) == [(30, 990)]


async def test_invoice_requires_token_and_price(maker, tgbot, monkeypatch):
    await set_price(maker, 30, 990)
    cb = MagicMock()
    cb.data, cb.from_user.id = "pay:30", 50
    cb.answer, cb.bot.send_invoice = AsyncMock(), AsyncMock()
    monkeypatch.setattr(config, "PAYMENT_PROVIDER_TOKEN", "")
    await ph.invoice(cb, SimpleNamespace(tg_id=50))
    cb.bot.send_invoice.assert_not_called()  # оплата выключена
    monkeypatch.setattr(config, "PAYMENT_PROVIDER_TOKEN", "tok")
    await ph.invoice(cb, SimpleNamespace(tg_id=50))
    kwargs = cb.bot.send_invoice.call_args.kwargs
    assert kwargs["prices"][0].amount == 99000 and kwargs["payload"] == "plan:30"
    cb.data = "pay:90"  # цена не задана — не продаётся
    cb.bot.send_invoice.reset_mock()
    await ph.invoice(cb, SimpleNamespace(tg_id=50))
    cb.bot.send_invoice.assert_not_called()


async def test_pre_checkout_checks_price(maker):
    await set_price(maker, 30, 990)
    good = SimpleNamespace(invoice_payload="plan:30", total_amount=99000, answer=AsyncMock())
    await ph.pre_checkout(good)
    assert good.answer.call_args.kwargs["ok"] is True
    stale = SimpleNamespace(invoice_payload="plan:30", total_amount=50000, answer=AsyncMock())
    await ph.pre_checkout(stale)
    assert stale.answer.call_args.kwargs["ok"] is False


async def test_payment_extends_plan_once_and_pays_referrer(maker, tgbot):
    await make_user(maker, 10)  # пригласивший
    await make_user(maker, 50, referred_by=10, ref_rewarded=True)
    m, user = paid_msg(50, 30, 99000)
    await ph.paid(m, user)
    await ph.paid(m, user)  # Telegram повторил уведомление — второй раз не продлеваем
    async with maker() as s:
        u, ref = await s.get(User, 50), await s.get(User, 10)
        assert await s.scalar(select(func.count()).select_from(Payment)) == 1
    assert u.plan == "full" and timedelta(days=29, hours=23) < u.plan_until - now() < timedelta(days=30, hours=1)
    assert timedelta(days=5) < ref.plan_until - now() < timedelta(days=6, hours=2)  # 20% от 30 дней = 6 дней
    assert any("Оплата" in t for t in tgbot.texts(ADMIN_ID))
    assert any("тариф" in c.args[0] for c in m.answer.call_args_list)


async def test_payment_stacks_on_existing_plan(maker, tgbot):
    await make_user(maker, 51, plan="full", plan_until=now() + timedelta(days=10))
    m, user = paid_msg(51, 30, 99000, "ch2")
    await ph.paid(m, user)
    async with maker() as s:
        u = await s.get(User, 51)
    assert timedelta(days=39) < u.plan_until - now() < timedelta(days=41)


async def test_public_stats_has_no_personal_data(maker, tgbot, monkeypatch):
    await make_user(maker, 60, city="Махачкала")
    a = AuthorInfo(user_id=777, username="secret_client", first_name="Секретный")
    await intake.handle(Incoming(text="Ищу кто сделает лендинг для магазина в Махачкале", author=a,
                                 group_tg_id=-1, group_title="Закрытая группа", message_id=1))
    text = await public_stats.build()
    assert "Найдено заявок: 1" in text and "Сайты" in text and "Махачкала" in text
    for private in ("secret_client", "Секретный", "Закрытая группа", "лендинг"):
        assert private not in text
    monkeypatch.setattr(config, "STATS_CHANNEL", "@my_stats")
    assert await public_stats.publish(tgbot)
    assert tgbot.texts("@my_stats")
    monkeypatch.setattr(config, "STATS_CHANNEL", "")
    assert not await public_stats.publish(tgbot)


async def test_prices_visible_before_payments_connected(maker, tgbot, monkeypatch):
    from app.bot.handlers import profile

    await set_price(maker, 30, 990)
    monkeypatch.setattr(config, "PAYMENT_PROVIDER_TOKEN", "")
    cb = MagicMock()
    cb.answer, cb.message.edit_text, cb.message.answer = AsyncMock(), AsyncMock(), AsyncMock()
    await profile.pf_plan(cb, SimpleNamespace(tg_id=50, plan="free", plan_until=None, is_admin=False))
    shown = cb.answer.call_args
    text, markup = shown.args[0], shown.kwargs["reply_markup"]
    assert "990" in str(markup) and "подключаем" in text
