"""Покупка 💎 Полного тарифа через Telegram Payments."""
import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, LabeledPrice, Message, PreCheckoutQuery
from sqlalchemy import select

from app import config
from app.bot import notify
from app.bot.common import esc, kb
from app.db import Session
from app.models import Payment, User
from app.services import payments

log = logging.getLogger(__name__)
router = Router()  # личные сообщения и кнопки (с пользователем)
checkout = Router()  # pre_checkout_query приходит без чата — отдельный роутер, без пользователя


@router.callback_query(F.data.regexp(r"^pay:\d+$"))
async def invoice(cb: CallbackQuery, user: User) -> None:
    if not payments.enabled():
        return await cb.answer("Оплата пока не подключена", show_alert=True)
    days = int(cb.data.split(":")[1])
    async with Session() as s:
        price = await payments.price_of(s, days)
    if not price:
        return await cb.answer("Этот пакет сейчас недоступен", show_alert=True)
    await cb.bot.send_invoice(
        chat_id=cb.from_user.id, title=f"💎 Полный тариф на {days} дн.",
        description="Заявки приходят сразу и без дневного лимита.",
        payload=f"plan:{days}", provider_token=config.PAYMENT_PROVIDER_TOKEN, currency=config.PAYMENT_CURRENCY,
        prices=[LabeledPrice(label=f"{days} дн.", amount=price * 100)],
    )
    await cb.answer()


@checkout.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery) -> None:
    days = int(q.invoice_payload.split(":")[1]) if q.invoice_payload.startswith("plan:") else 0
    async with Session() as s:
        ok = days in dict(await payments.packages(s)) and q.total_amount == await payments.price_of(s, days) * 100
    await q.answer(ok=ok, error_message=None if ok else "Цена изменилась — откройте тариф заново.")


@router.message(F.successful_payment)
async def paid(m: Message, user: User) -> None:
    p = m.successful_payment
    days = int(p.invoice_payload.split(":")[1])
    async with Session() as s:
        new, share = await payments.apply(s, user.tg_id, days, p.total_amount, p.currency,
                                          p.telegram_payment_charge_id, p.provider_payment_charge_id or "")
        row = await s.get(User, user.tg_id)
        pay_id = await s.scalar(select(Payment.id).where(Payment.charge_id == p.telegram_payment_charge_id))
    if not new:
        return
    until = f" до {row.plan_until:%d.%m.%Y}" if row.plan_until else " бессрочно"
    await m.answer(f"✅ Оплата получена. 💎 Полный тариф{until}. Спасибо!")
    await notify._send(f"💰 Оплата: {esc(user.name or str(user.tg_id))} — {p.total_amount / 100:.0f} {p.currency}, +{days} дн.",
                       kb([[("↩️ Вернуть и снять тариф", f"rf:ask:{pay_id}")]]))
    if share:
        await notify.bonus(share[0], share[1], "ref")


async def _contact(s) -> str:
    admin = await s.scalar(select(User).where(User.is_admin, User.username != "").limit(1))
    return f"@{admin.username}" if admin else "администратору бота"


@router.message(Command("terms"))
async def terms(m: Message) -> None:
    async with Session() as s:
        offers = await payments.packages(s)
        contact = await _contact(s)
    prices = "\n".join(f"• {d} дн. — {p} ₽" for d, p in offers) or "• цены уточняйте у администратора"
    await m.answer(
        "📄 <b>Условия</b>\n\n"
        "Услуга: 💎 Полный тариф Telegram-бота «КлиентРадар» — заявки приходят сразу и без дневного лимита. "
        "Это цифровая услуга: доступ включается автоматически сразу после оплаты на выбранный срок.\n\n"
        f"Стоимость:\n{prices}\n\n"
        "Бот присылает сообщения людей из открытых Telegram-групп; мы не гарантируем количество заявок и сделок.\n"
        "Если окно оплаты не открывается (например, из-за VPN), отключите VPN или выберите сервер в России и повторите; "
        "можно также оплатить через администратора.\n"
        f"Вопросы по оплате и возврату: /paysupport или {contact}.")


@router.message(Command("paysupport"))
async def paysupport(m: Message) -> None:
    async with Session() as s:
        contact = await _contact(s)
    await m.answer(
        "💬 <b>Вопросы по оплате</b>\n\n"
        f"Напишите {contact} и укажите свой ID: <code>{m.from_user.id}</code>, дату оплаты и суть вопроса. "
        "Ответим как можно быстрее; возврат рассматривается по обращению.")


# ---------- возврат (только администратор) ----------
async def _load(cb: CallbackQuery, user: User):
    """Платёж и пользователь, либо None (с ответом администратору)."""
    if not user.is_admin:
        await cb.answer()
        return None
    async with Session() as s:
        pay = await s.get(Payment, int(cb.data.split(":")[2]))
        buyer = await s.get(User, pay.user_id) if pay else None
    if pay is None:
        await cb.answer("Платёж не найден", show_alert=True)
        return None
    return pay, buyer


@router.callback_query(F.data.regexp(r"^rf:ask:\d+$"))
async def refund_ask(cb: CallbackQuery, user: User) -> None:
    loaded = await _load(cb, user)
    if loaded is None:
        return
    pay, buyer = loaded
    if pay.refunded:
        return await cb.answer("Этот платёж уже возвращён", show_alert=True)
    who = esc((buyer.name if buyer else "") or str(pay.user_id))
    rows = []
    if payments.refunds_enabled() and pay.provider_id:
        rows.append([("💸 Вернуть деньги и снять тариф", f"rf:money:{pay.id}")])
    rows.append([("Только снять тариф (деньги верну сам)", f"rf:tariff:{pay.id}")])
    rows.append([("Отмена", "noop")])
    hint = "" if payments.refunds_enabled() else "\n\nВозврат денег кнопкой не подключён (ключи ЮKassa в .env) — деньги верните в кабинете ЮKassa."
    await cb.message.answer(
        f"↩️ Платёж: {who} — {pay.amount / 100:.0f} {esc(pay.currency)}, {pay.days} дн.{hint}", reply_markup=kb(rows))
    await cb.answer()


@router.callback_query(F.data.regexp(r"^rf:(money|tariff):\d+$"))
async def refund_do(cb: CallbackQuery, user: User) -> None:
    loaded = await _load(cb, user)
    if loaded is None:
        return
    pay, buyer = loaded
    money = cb.data.split(":")[1] == "money"
    async with Session() as s:
        pay = await s.get(Payment, pay.id)
        try:
            note = await payments.refund(s, pay, money)
        except payments.RefundError as e:
            return await cb.answer(str(e), show_alert=True)
    await cb.message.edit_text(f"✅ Готово. Тариф снят. {note}")
    await cb.answer()
    try:
        await cb.bot.send_message(pay.user_id, "↩️ Платёж за 💎 Полный тариф возвращён, тариф отключён."
                                  if money else "ℹ️ Ваш 💎 Полный тариф отключён администратором.")
    except Exception:
        pass
