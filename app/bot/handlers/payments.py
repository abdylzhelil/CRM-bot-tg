"""Покупка 💎 Полного тарифа через Telegram Payments."""
import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery, LabeledPrice, Message, PreCheckoutQuery
from sqlalchemy import select

from app import config
from app.bot import notify
from app.bot.common import esc
from app.db import Session
from app.models import User
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
                                          p.telegram_payment_charge_id)
        row = await s.get(User, user.tg_id)
    if not new:
        return
    until = f" до {row.plan_until:%d.%m.%Y}" if row.plan_until else " бессрочно"
    await m.answer(f"✅ Оплата получена. 💎 Полный тариф{until}. Спасибо!")
    await notify._send(f"💰 Оплата: {esc(user.name or str(user.tg_id))} — {p.total_amount / 100:.0f} {p.currency}, +{days} дн.")
    if share:
        await notify.bonus(share[0], share[1], "ref")
