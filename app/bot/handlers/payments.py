"""Покупка 💎 Полного тарифа через Telegram Payments."""
import logging

from aiogram import F, Router
from aiogram.filters import Command
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
        f"Вопросы по оплате и возврату: /paysupport или {contact}.")


@router.message(Command("paysupport"))
async def paysupport(m: Message) -> None:
    async with Session() as s:
        contact = await _contact(s)
    await m.answer(
        "💬 <b>Вопросы по оплате</b>\n\n"
        f"Напишите {contact} и укажите свой ID: <code>{m.from_user.id}</code>, дату оплаты и суть вопроса. "
        "Ответим как можно быстрее; возврат рассматривается по обращению.")
