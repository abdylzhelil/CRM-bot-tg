"""Этап 5: оплата тарифа через Telegram Payments и публичная статистика."""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy import func, select

from app import config
from app.bot.handlers import payments as ph
from app.models import Payment, PendingLink, User, now
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
                                           telegram_payment_charge_id=charge,
                                           provider_payment_charge_id="yk-" + charge)
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
    await profile.pf_plan(cb, SimpleNamespace(tg_id=50, plan="free", plan_until=None, is_admin=False, renew_method="", renew_days=0))
    shown = cb.answer.call_args
    text, markup = shown.args[0], shown.kwargs["reply_markup"]
    assert "990" in str(markup) and "подключаем" in text


# ---------- возврат ----------
async def _paid_user(maker, uid=60, days=30, charge="chR"):
    await make_user(maker, uid)
    m, user = paid_msg(uid, days, 99000, charge)
    await ph.paid(m, user)
    async with maker() as s:
        return await s.scalar(select(Payment).where(Payment.charge_id == charge))


def cbq(data, admin=True):
    c = MagicMock()
    c.data = data
    c.answer = AsyncMock()
    c.message.answer, c.message.edit_text = AsyncMock(), AsyncMock()
    c.bot.send_message = AsyncMock()
    return c


async def test_refund_money_and_tariff(maker, tgbot, monkeypatch):
    monkeypatch.setattr(config, "YOOKASSA_SHOP_ID", "1492675")
    monkeypatch.setattr(config, "YOOKASSA_SECRET_KEY", "secret")
    pay = await _paid_user(maker)
    assert pay.provider_id == "yk-chR"
    calls = []

    async def fake_yookassa(payment_id, amount, currency, key):
        calls.append((payment_id, amount, currency, key))
        return {"status": "succeeded"}

    monkeypatch.setattr(payments, "_yookassa_refund", fake_yookassa)
    admin = SimpleNamespace(tg_id=ADMIN_ID, is_admin=True)
    await ph.refund_ask(cbq(f"rf:ask:{pay.id}"), admin)
    c = cbq(f"rf:money:{pay.id}")
    await ph.refund_do(c, admin)
    assert calls == [("yk-chR", 99000, "RUB", f"refund-{pay.id}")]
    async with maker() as s:
        u, p = await s.get(User, 60), await s.get(Payment, pay.id)
    assert p.refunded and u.plan == "free" and u.plan_until is None  # дни платежа сняты
    c.bot.send_message.assert_awaited()  # человеку сообщили
    again = cbq(f"rf:money:{pay.id}")
    await ph.refund_do(again, admin)  # повторно нельзя
    assert "уже возвращён" in again.answer.call_args.args[0] and len(calls) == 1


async def test_refund_only_tariff_keeps_other_days(maker, tgbot):
    pay = await _paid_user(maker, uid=61, days=30, charge="chA")
    m, user = paid_msg(61, 90, 129000, "chB")  # вторая оплата: 90 дней
    async with maker() as s:
        await settings.put(s, "price_90", "1290")
    await ph.paid(m, user)
    admin = SimpleNamespace(tg_id=ADMIN_ID, is_admin=True)
    await ph.refund_do(cbq(f"rf:tariff:{pay.id}"), admin)  # снимаем только первые 30 дней
    async with maker() as s:
        u = await s.get(User, 61)
    assert timedelta(days=89) < u.plan_until - now() < timedelta(days=91)


async def test_refund_failures_and_access(maker, tgbot, monkeypatch):
    pay = await _paid_user(maker, uid=62, charge="chF")
    admin = SimpleNamespace(tg_id=ADMIN_ID, is_admin=True)
    c = cbq(f"rf:money:{pay.id}")
    await ph.refund_do(c, admin)  # ключей нет
    assert "Ключи ЮKassa" in c.answer.call_args.args[0]
    monkeypatch.setattr(config, "YOOKASSA_SHOP_ID", "1")
    monkeypatch.setattr(config, "YOOKASSA_SECRET_KEY", "k")

    async def refuse(*a):
        return {"status": "canceled", "description": "Insufficient funds"}

    monkeypatch.setattr(payments, "_yookassa_refund", refuse)
    c = cbq(f"rf:money:{pay.id}")
    await ph.refund_do(c, admin)
    assert "отказала" in c.answer.call_args.args[0]
    async with maker() as s:
        assert not (await s.get(Payment, pay.id)).refunded  # при отказе тариф не снят
        assert (await s.get(User, 62)).plan == "full"
    c = cbq(f"rf:tariff:{pay.id}")  # обычный пользователь не может
    await ph.refund_do(c, SimpleNamespace(tg_id=62, is_admin=False))
    async with maker() as s:
        assert not (await s.get(Payment, pay.id)).refunded


# ---------- оплата по ссылке (СБП) ----------
class FakeYK:
    """Подмена API ЮKassa: хранит платежи и отвечает на создание/проверку."""

    def __init__(self):
        self.payments, self.calls = {}, []

    async def __call__(self, method, path, body=None, key=""):
        self.calls.append((method, path, body))
        if method == "POST":
            pid = f"yk-{len(self.payments) + 1}"
            self.payments[pid] = {"id": pid, "status": "pending", "paid": False, "amount": body["amount"]}
            return {"id": pid, "confirmation": {"confirmation_url": f"https://yookassa.ru/pay/{pid}"}, "_http": 200}
        return dict(self.payments[path.rsplit("/", 1)[1]], _http=200)


async def test_link_payment_flow(maker, tgbot, monkeypatch):
    monkeypatch.setattr(config, "YOOKASSA_SHOP_ID", "1492675")
    monkeypatch.setattr(config, "YOOKASSA_SECRET_KEY", "k")
    await set_price(maker, 30, 490)
    await make_user(maker, 70)
    yk = FakeYK()
    async with maker() as s:
        url = await payments.create_link(s, 70, 30, "https://t.me/klientradarbot", api=yk)
    assert url == "https://yookassa.ru/pay/yk-1"
    method, path, body = yk.calls[0]
    # СБП появится только при одностадийной оплате и без принудительного способа оплаты
    assert body["capture"] is True and "payment_method_data" not in body and body["amount"]["value"] == "490.00"
    assert await ph.process_links(yk) == 0  # ещё не оплачено
    async with maker() as s:
        assert (await s.get(User, 70)).plan == "free"
    yk.payments["yk-1"].update(status="succeeded", paid=True)
    assert await ph.process_links(yk) == 1
    assert await ph.process_links(yk) == 0  # повторно не засчитывается
    async with maker() as s:
        u = await s.get(User, 70)
        pay = await s.scalar(select(Payment).where(Payment.user_id == 70))
    assert u.plan == "full" and timedelta(days=29, hours=23) < u.plan_until - now() < timedelta(days=30, hours=1)
    assert pay.provider_id == "yk-1" and pay.amount == 49000  # номер ЮKassa сохранён — возврат кнопкой возможен
    assert any("Оплата получена" in t for t in tgbot.texts(70)) and any("Оплата:" in t for t in tgbot.texts(ADMIN_ID))


async def test_link_canceled_and_expired(maker, tgbot, monkeypatch):
    monkeypatch.setattr(config, "YOOKASSA_SHOP_ID", "1")
    monkeypatch.setattr(config, "YOOKASSA_SECRET_KEY", "k")
    await set_price(maker, 30, 490)
    await make_user(maker, 71)
    yk = FakeYK()
    async with maker() as s:
        await payments.create_link(s, 71, 30, "https://t.me/x", api=yk)
        await payments.create_link(s, 71, 30, "https://t.me/x", api=yk)
    yk.payments["yk-1"]["status"] = "canceled"
    assert await ph.process_links(yk) == 0
    async with maker() as s:
        rows = {r.yk_id: r.done for r in (await s.scalars(select(PendingLink))).all()}
        assert rows == {"yk-1": True, "yk-2": False}
        (await s.get(PendingLink, 2)).created_at = now() - timedelta(hours=5)
        await s.commit()
    assert await ph.process_links(yk) == 0
    async with maker() as s:
        assert all(r.done for r in (await s.scalars(select(PendingLink))).all())  # просроченные закрыты
        assert (await s.get(User, 71)).plan == "free"


async def test_link_requires_keys_and_price(maker, tgbot, monkeypatch):
    monkeypatch.setattr(config, "YOOKASSA_SHOP_ID", "")
    monkeypatch.setattr(config, "YOOKASSA_SECRET_KEY", "")
    c = cbq("lnk:30")
    await ph.link_pay(c, SimpleNamespace(tg_id=5))
    assert "не подключена" in c.answer.call_args.args[0]
    monkeypatch.setattr(config, "YOOKASSA_SHOP_ID", "1")
    monkeypatch.setattr(config, "YOOKASSA_SECRET_KEY", "k")
    c = cbq("lnk:30")
    c.bot.me = AsyncMock(return_value=SimpleNamespace(username="bot"))
    await ph.link_pay(c, SimpleNamespace(tg_id=5))  # цена 0 — пакет недоступен
    assert "недоступен" in c.answer.call_args.args[0]


async def test_terms_show_refund_period(maker, tgbot):
    async with maker() as s:
        await settings.put(s, "price_30", "490")
    m = MagicMock()
    m.from_user.id = 5
    m.answer = AsyncMock()
    await ph.terms(m)
    assert "в течение 3 дн." in m.answer.call_args.args[0]  # срок по умолчанию
    async with maker() as s:
        await settings.put(s, "refund_days", "7")
    await ph.paysupport(m)
    assert "в течение 7 дн." in m.answer.call_args.args[0]
    async with maker() as s:
        await settings.put(s, "refund_days", "0")
    await ph.terms(m)
    assert "не предусмотрен" in m.answer.call_args.args[0]
