"""Покупка 💎 Полного тарифа через Telegram Payments."""
import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, LabeledPrice, Message, PreCheckoutQuery
from sqlalchemy import select

from app import config
from app.bot import notify
from app.bot.common import alert, esc, kb
from app.db import Session
from app.models import Payment, PendingLink, User
from app.services import payments, settings

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
    # чем длиннее тариф, тем больше «целей» на радаре: 30 дн. — 3, до 90 — 6, дольше — 10
    tier = 30 if days <= 30 else 90 if days <= 90 else 365
    extra = {"photo_url": f"{config.WEBAPP_URL}/app/invoice_{tier}.png", "photo_width": 640, "photo_height": 360} if config.WEBAPP_URL else {}
    await cb.bot.send_invoice(
        chat_id=cb.from_user.id, title=f"💎 Полный тариф на {days} дн.",
        description="📡 КлиентРадар: заявки от клиентов приходят сразу и без дневного лимита, своя CRM и кабинет в Telegram. "
                    "Тариф включится сам сразу после оплаты.",
        payload=f"plan:{days}", provider_token=config.PAYMENT_PROVIDER_TOKEN, currency=config.PAYMENT_CURRENCY,
        prices=[LabeledPrice(label=f"Полный тариф, {days} дн.", amount=price * 100)], **extra,
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
    await _announce(row, user.name or str(user.tg_id), days, p.total_amount, p.currency, pay_id, share, m.answer)


async def _announce(row, name: str, days: int, amount: int, currency: str, pay_id, share, reply) -> None:
    """Сообщения после успешной оплаты: человеку, администратору (с кнопкой возврата), пригласившему."""
    until = f" до {row.plan_until:%d.%m.%Y}" if row.plan_until else " бессрочно"
    from app.bot import effects

    await effects.send(reply, f"✅ Оплата получена. 💎 Полный тариф{until}. Спасибо!")
    await notify._send(f"💰 Оплата: {esc(name)} — {amount / 100:.0f} {currency}, +{days} дн.\n🧾 Не забудьте чек в «Мой налог».",
                       kb([[("↩️ Вернуть и снять тариф", f"rf:ask:{pay_id}")]]))
    if share:
        await notify.bonus(share[0], share[1], "ref")


# ---------- оплата по ссылке: СБП и карта в браузере ----------
@router.callback_query(F.data == "lnk:menu")
async def link_menu(cb: CallbackQuery, user: User) -> None:
    if not payments.links_enabled():
        return await cb.answer("Оплата по ссылке пока не подключена", show_alert=True)
    async with Session() as s:
        offers = await payments.packages(s)
    async with Session() as s:
        auto = await settings.get(s, "autorenew") == "1"
    rows = [[(f"📱 {d} дн. — {price} ₽", f"lnk:{d}")] + ([(f"🔁 с автопродлением", f"lnk:{d}:auto")] if auto else [])
            for d, price in offers] + [[("◀️ Тариф", "pf:plan")]]
    from app.bot.common import reply

    extra = ("\n\n🔁 <b>С автопродлением</b>: ЮKassa запомнит карту, и перед окончанием тарифа мы сами спишем ту же сумму. "
             "За 3 дня предупредим, отключить можно в любой момент: 👤 Профиль → 💎 Тариф." if auto else "")
    await reply(cb, "📱 <b>Оплата по ссылке</b>\nОткроется страница ЮKassa: можно заплатить через <b>СБП</b> "
                "(по номеру телефона, без комиссии для вас как покупателя) или картой. "
                "Тариф включится сам в течение минуты после оплаты." + extra, kb(rows))


@router.callback_query(F.data.regexp(r"^lnk:\d+(:auto)?$"))
async def link_pay(cb: CallbackQuery, user: User) -> None:
    if not payments.links_enabled():
        return await cb.answer("Оплата по ссылке пока не подключена", show_alert=True)
    days = int(cb.data.split(":")[1])
    auto = cb.data.endswith(":auto")
    me = await cb.bot.me()
    async with Session() as s:
        if auto and await settings.get(s, "autorenew") != "1":
            return await cb.answer("Автопродление сейчас недоступно", show_alert=True)
        try:
            url = await payments.create_link(s, user.tg_id, days, f"https://t.me/{me.username}", auto=auto)
        except payments.RefundError as e:
            return await alert(cb, str(e))
    await cb.message.answer(
        f"🔗 Счёт на {days} дн. создан{' с автопродлением' if auto else ''}. Нажмите кнопку, оплатите, и вернитесь в бот — тариф включится сам.",
        reply_markup=kb([[("Оплатить (СБП / карта)", f"url:{url}")]]))
    await cb.answer()


async def process_links(api=None) -> int:
    """Проверить ожидающие платежи по ссылкам и включить тариф тем, кто заплатил. Возвращает число обработанных."""
    done = 0
    async with Session() as s:
        found = await payments.check_links(s, api)
    for row, data in found:
        async with Session() as s:
            link = await s.get(PendingLink, row.id)
            if link is None or link.done:
                continue
            link.done = True
            if link.order_id:  # оплата рекламы, а не тарифа
                from app.services import adposts

                paid = await adposts.mark_paid(s, link.order_id, link.yk_id, link.amount, (data.get("amount") or {}).get("currency", "RUB"))
                await s.commit()
                if paid is not None:
                    await _ad_paid(paid[0].id, paid[0].user_id, link.amount, paid[1].id)
                    done += 1
                    continue
                extra = await adposts.record_extra_payment(s, link.order_id, link.user_id, link.yk_id, link.amount)
                if extra is not None:  # лишняя/поздняя оплата: деньги не теряем, админу — кнопка возврата
                    await _ad_extra(link.order_id, link.user_id, link.amount, extra.id)
                    done += 1
                continue
            amount = int(round(float((data.get("amount") or {}).get("value", link.amount / 100)) * 100))
            new, share = await payments.apply(s, link.user_id, link.days, amount, (data.get("amount") or {}).get("currency", "RUB"),
                                              f"yk:{link.yk_id}", link.yk_id)
            u = await s.get(User, link.user_id)
            method = data.get("payment_method") or {}
            if link.auto and u is not None and method.get("saved") and method.get("id"):
                u.renew_method, u.renew_days, u.renew_card = method["id"], link.days, payments.card_label(method)
            pay_id = await s.scalar(select(Payment.id).where(Payment.charge_id == f"yk:{link.yk_id}"))
            await s.commit()
        if not new or u is None:
            continue

        async def reply(text, uid=u.tg_id):
            await notify._bot.send_message(uid, text)

        await _announce(u, u.name or str(u.tg_id), link.days, amount, "RUB", pay_id, share, reply)
        done += 1
    return done


async def _ad_paid(order_id: int, user_id: int, amount: int, pay_id: int) -> None:
    try:
        await notify._bot.send_message(user_id, f"✅ Оплата получена. Заказ рекламы №{order_id} поставлен в очередь: "
                                       "когда посты выйдут, пришлю ссылки.")
    except Exception:
        pass
    await notify._send(f"💰 Оплачена реклама, заказ №{order_id}: {amount / 100:.0f} ₽.\n🧾 Не забудьте чек в «Мой налог».",
                       kb([[("↩️ Вернуть и отменить посты", f"rf:ask:{pay_id}")]]))


async def _ad_extra(order_id: int, user_id: int, amount: int, pay_id: int) -> None:
    try:
        await notify._bot.send_message(user_id, f"ℹ️ Пришла оплата {amount / 100:.0f} ₽ по заказу рекламы №{order_id}, но он уже оплачен "
                                       "или отменён. Деньги вернём — администратор уже получил уведомление.")
    except Exception:
        pass
    await notify._send(f"⚠️ Лишняя оплата рекламы: заказ №{order_id} уже оплачен или отменён, а пришло ещё {amount / 100:.0f} ₽. "
                       "Верните деньги кнопкой ниже.", kb([[("↩️ Вернуть деньги", f"rf:ask:{pay_id}")]]))


@router.callback_query(F.data == "ren:ask")
async def renew_ask(cb: CallbackQuery, user: User) -> None:
    """Подтверждение отвязки карты (чтобы не отвязать случайным нажатием)."""
    async with Session() as s:
        row = await s.get(User, user.tg_id)
    if not row or not row.renew_method:
        return await cb.answer("Карта не привязана", show_alert=True)
    from app.bot.common import reply

    await reply(cb, (f"🗑 <b>Отвязать карту?</b>\n💳 {row.renew_card or 'Банковская карта'}\n\n"
                     "После отвязки автопродление отключится, деньги с карты больше списываться не будут. "
                     "Оплаченный тариф продолжит действовать до конца срока."),
                kb([[("✅ Да, отвязать карту", "ren:off")], [("Отмена", "pf:plan")]]))


@router.callback_query(F.data == "ren:off")
async def renew_off(cb: CallbackQuery, user: User) -> None:
    from app.bot.common import reply

    async with Session() as s:
        row = await s.get(User, user.tg_id)
        payments.disable_renew(row)
        await s.commit()
    await cb.answer("Карта отвязана")
    await reply(cb, "✅ <b>Карта отвязана.</b>\nАвтопродление отключено, деньги с карты больше списываться не будут. "
                "Оплаченный тариф действует до конца срока. Продлить вручную: 💎 Тариф.", kb([[("💎 Тариф", "pf:plan")]]))


async def renew_once() -> int:
    """Продление тем, у кого включено автопродление (вызывается циклом раз в час)."""
    async def tell(uid, text, good):
        await notify._bot.send_message(uid, text)

    async def tell_admin(u, days, amount, yk_id, share):
        await notify._send(f"💰 Автопродление: {esc(u.name or str(u.tg_id))} — {amount / 100:.0f} RUB, +{days} дн.\n🧾 Не забудьте чек в «Мой налог».")
        if share:
            await notify.bonus(share[0], share[1], "ref")

    async with Session() as s:
        return await payments.renew_due(s, notify_user=tell, notify_admin=tell_admin)


async def link_loop() -> None:
    import asyncio

    while True:
        try:
            if payments.links_enabled():
                await process_links()
                await renew_once()
        except Exception:
            log.exception("link_loop")
        await asyncio.sleep(15)


def refund_line(days: int) -> str:
    if days <= 0:
        return "Возврат оплаченного тарифа не предусмотрен; если что-то не работает — напишите нам, разберёмся."
    return (f"Если вы не довольны, мы вернём деньги в течение {days} дн. после оплаты, пока тарифом ещё не пользовались "
            "(не получали заявки по платному тарифу). Напишите нам: /paysupport.")


async def _contact(s) -> str:
    admin = await s.scalar(select(User).where(User.is_admin, User.username != "").limit(1))
    return f"@{admin.username}" if admin else "администратору бота"


@router.message(Command("terms"))
async def terms(m: Message) -> None:
    async with Session() as s:
        offers = await payments.packages(s)
        contact = await _contact(s)
        refund = refund_line(await settings.get_int(s, "refund_days"))
    prices = "\n".join(f"• {d} дн. — {p} ₽" for d, p in offers) or "• цены уточняйте у администратора"
    await m.answer(
        "📄 <b>Условия</b>\n\n"
        "Услуга: 💎 Полный тариф Telegram-бота «КлиентРадар» — заявки приходят сразу и без дневного лимита. "
        "Это цифровая услуга: доступ включается автоматически сразу после оплаты на выбранный срок.\n\n"
        f"Стоимость:\n{prices}\n\n"
        "Бот присылает сообщения людей из открытых Telegram-групп; мы не гарантируем количество заявок и сделок.\n"
        "Если окно оплаты не открывается (например, из-за VPN), отключите VPN или выберите сервер в России и повторите; "
        "можно также оплатить через администратора.\n"
        f"<b>Возврат.</b> {refund}\n"
        f"Вопросы по оплате и возврату: /paysupport или {contact}.")


@router.message(Command("paysupport"))
async def paysupport(m: Message) -> None:
    async with Session() as s:
        contact = await _contact(s)
        refund = refund_line(await settings.get_int(s, "refund_days"))
    await m.answer(
        "💬 <b>Вопросы по оплате</b>\n\n"
        f"Напишите {contact} и укажите свой ID: <code>{m.from_user.id}</code>, дату оплаты и суть вопроса. "
        f"Ответим как можно быстрее. {refund}")


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
        rows.append([("💸 Вернуть деньги и снять тариф" if pay.days else "💸 Вернуть деньги и отменить посты", f"rf:money:{pay.id}")])
    rows.append([("Только снять тариф (деньги верну сам)" if pay.days else "Только отменить посты (деньги верну сам)", f"rf:tariff:{pay.id}")])
    rows.append([("Отмена", "noop")])
    hint = "" if payments.refunds_enabled() else "\n\nВозврат денег кнопкой не подключён (ключи ЮKassa в .env) — деньги верните в кабинете ЮKassa."
    part = (f" (уже вернули {pay.refunded_amount / 100:.0f}, осталось вернуть {(pay.amount - pay.refunded_amount) / 100:.0f})"
            if pay.refunded_amount else "")
    await cb.message.answer(
        f"↩️ Платёж: {who} — {pay.amount / 100:.0f} {esc(pay.currency)}{part}, "
        + (f"{pay.days} дн." if pay.days else "реклама в группах (неопубликованные посты будут отменены)") + hint, reply_markup=kb(rows))
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
            return await alert(cb, str(e))
    await cb.message.edit_text(f"✅ Готово. {'Тариф снят.' if pay.days else 'Неопубликованные посты отменены.'} {note}")
    await cb.answer()
    try:
        if not pay.days:
            text = ("↩️ Платёж за рекламу возвращён, неопубликованные посты отменены." if money
                    else "ℹ️ Неопубликованные посты вашей рекламы отменены администратором.")
        else:
            text = ("↩️ Платёж за 💎 Полный тариф возвращён, тариф отключён." if money
                    else "ℹ️ Ваш 💎 Полный тариф отключён администратором.")
        await cb.bot.send_message(pay.user_id, text)
    except Exception:
        pass
