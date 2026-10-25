"""💰 Кредиты: «💰 Мои кредиты» у человека и «💰 Кредиты» у админа (🛠 Управление). Логика — services/credits.py.

Человек: баланс и история, обмен на дни 💎 (`cr:days` → `cr:d:<дней>` → `cr:dok:<дней>`), на код бота модерации
(`cr:mod` → `cr:m:<тариф>` → `cr:mok:<тариф>`), отмена ждущей заявки на код (`cr:cancel:<id>`).
Админ: запас кодов (`cra:add:<тариф>` → ввод `cra_codes`, `cra:codes`, `cra:del:<id>`), ждущие заявки
(`cra:wait`, `cra:ret:<id>`), ручное начисление/списание (`cra:adj` → ввод `cra_adj`).
"""
import asyncio
import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app import config
from app.bot import notify
from app.bot.common import alert, ask, esc, kb, reply
from app.db import Session
from app.models import ModCode, User
from app.services import credits, settings

router = Router()        # всем
admin_router = Router()  # админу
log = logging.getLogger(__name__)
_lock = asyncio.Lock()   # двойное нажатие «купить» не спишет дважды


async def _mod_bot(s) -> str:
    return (await settings.get(s, "mod_bot_username") or "").lstrip("@")


def code_text(row: ModCode, bot_name: str) -> str:
    title = credits.MOD_KINDS.get(row.kind, (row.kind, ""))[0]
    where = f"@{bot_name}" if bot_name else "боте модерации"
    return (f"🔑 <b>Ваш код: {title}</b>\n<code>{esc(row.code)}</code>\n\n"
            f"Как включить: откройте {where} → добавьте его админом в свою группу → в меню группы "
            "«🔑 Ввести код активации» → пришлите этот код.")


def code_markup(row: ModCode, bot_name: str):
    rows = [[("📋 Скопировать код", f"copy:{row.code}")]]
    if bot_name:
        rows.append([("🛡 Открыть бота модерации", f"url:https://t.me/{bot_name}")])
    return kb(rows)


async def send_code(row: ModCode) -> None:
    async with Session() as s:
        name = await _mod_bot(s)
    try:
        await notify._bot.send_message(row.user_id, code_text(row, name), reply_markup=code_markup(row, name))
    except Exception:
        log.info("код бота модерации не доставлен %s", row.user_id)


# ---------- человек ----------
async def show(target, user: User) -> None:
    from app.bot.handlers import board

    async with Session() as s:
        row = await s.get(User, user.tg_id)
        hist = await credits.history(s, user.tg_id)
        waiting = await credits.waiting_of(s, user.tg_id)
        codes = await credits.codes_of(s, user.tg_id)
        pct = await settings.get_int(s, "board_percent")
    lines = [f"💰 <b>Мои кредиты: {row.credits or 0}</b>",
             "1 кредит = 1 ₽ внутри бота. Тратятся на дни 💎 и на бота модерации для вашей группы.", "",
             f"Как получить: поставьте КлиентРадар в свою группу админом — в закрепе появится рейтинг заявок. "
             f"Кто придёт по кнопке из закрепа и оплатит 💎, — вам {pct}% суммы кредитами."]
    if waiting:
        lines += ["", "⏳ Ждут кода бота модерации: " + ", ".join(
            credits.MOD_KINDS.get(w.kind, (w.kind,))[0].replace("🛡 ", "") for w in waiting)
            + " — пришлю, как только владелец добавит коды (обычно в течение суток)."]
    if codes:
        lines += ["", "🔑 Ваши коды бота модерации:"] + [
            f"• {credits.MOD_KINDS.get(c.kind, (c.kind,))[0].replace('🛡 ', '')}: <code>{esc(c.code)}</code>" for c in codes]
    lines += ["", "📜 <b>История</b>"] + ([esc(credits.tx_line(t)) for t in hist] or ["пока пусто"])
    rows = [[("💎 Дни за кредиты", "cr:days"), ("🛡 Бот модерации", "cr:mod")]]
    rows += [[(f"↩️ Отменить заявку на код (+{w.price})", f"cr:cancel:{w.id}")] for w in waiting]
    link = await board.add_link()
    if link:
        rows.append([("➕ Поставить бота в мою группу", f"url:{link}")])
    await reply(target, "\n".join(lines), kb(rows))


@router.callback_query(F.data == "cr:menu")
async def cr_menu(cb: CallbackQuery, user: User) -> None:
    await show(cb, user)


@router.message(Command("credits"))
async def cr_cmd(m: Message, user: User) -> None:
    await show(m, user)


@router.callback_query(F.data == "cr:days")
async def cr_days(cb: CallbackQuery, user: User) -> None:
    async with Session() as s:
        offers = await credits.day_offers(s)
        bal = (await s.get(User, user.tg_id)).credits or 0
    if not offers:
        return await alert(cb, "Дни 💎 сейчас не продаются.")
    rows = [[(f"{'✅' if bal >= p else '🔒'} {d} дн. 💎 — {p} кр.", f"cr:d:{d}")] for d, p in offers]
    await reply(cb, f"💎 <b>Дни полного тарифа за кредиты</b>\nНа счёте: {bal}. Выберите пакет:",
                kb(rows + [[("◀️ Назад", "cr:menu")]]))


@router.callback_query(F.data.regexp(r"^cr:d:\d+$"))
async def cr_day(cb: CallbackQuery, user: User) -> None:
    days = int(cb.data.split(":")[2])
    async with Session() as s:
        price = dict(await credits.day_offers(s)).get(days)
        bal = (await s.get(User, user.tg_id)).credits or 0
    if not price:
        return await alert(cb, "Этот пакет сейчас не продаётся.")
    if bal < price:
        return await alert(cb, f"Не хватает кредитов: нужно {price}, на счёте {bal}.")
    await reply(cb, f"Обменять {price} кредитов на {days} дн. 💎? Останется {bal - price}.",
                kb([[("✅ Да, обменять", f"cr:dok:{days}"), ("◀️ Назад", "cr:days")]]))


@router.callback_query(F.data.regexp(r"^cr:dok:\d+$"))
async def cr_day_ok(cb: CallbackQuery, user: User) -> None:
    days = int(cb.data.split(":")[2])
    async with _lock, Session() as s:
        row = await s.get(User, user.tg_id)
        try:
            price = await credits.buy_days(s, row, days)
        except credits.CreditError as e:
            return await alert(cb, str(e))
        await s.commit()
        until = row.plan_until
    tail = f" до {until:%d.%m.%Y}" if until else ""
    await reply(cb, f"✅ Готово: +{days} дн. 💎{tail}. Списано {price} кредитов.", kb([[("💰 Мои кредиты", "cr:menu")]]))


@router.callback_query(F.data == "cr:mod")
async def cr_mod(cb: CallbackQuery, user: User) -> None:
    async with Session() as s:
        offers = await credits.mod_offers(s)
        bal = (await s.get(User, user.tg_id)).credits or 0
        name = await _mod_bot(s)
    if not offers:
        return await alert(cb, "Бот модерации за кредиты пока не продаётся.")
    rows = [[(f"{'✅' if bal >= p else '🔒'} {t.replace('🛡 ', '')} — {p} кр.", f"cr:m:{k}")] for k, t, p, _ in offers]
    who = f" @{name}" if name else ""
    await reply(cb, f"🛡 <b>Бот модерации{who} за кредиты</b>\nУбирает спам, ботов и рекламу в вашей группе, капча "
                    f"для новичков. Вы получите код активации — его вводят в боте модерации.\nНа счёте: {bal}.",
                kb(rows + ([[("ℹ️ Что умеет бот модерации", f"url:https://t.me/{name}")]] if name else [])
                   + [[("◀️ Назад", "cr:menu")]]))


@router.callback_query(F.data.regexp(r"^cr:m:\w+$"))
async def cr_mod_one(cb: CallbackQuery, user: User) -> None:
    kind = cb.data.split(":")[2]
    async with Session() as s:
        offer = next((o for o in await credits.mod_offers(s) if o[0] == kind), None)
        bal = (await s.get(User, user.tg_id)).credits or 0
    if offer is None:
        return await alert(cb, "Этот тариф сейчас не продаётся.")
    _, title, price, stock = offer
    if bal < price:
        return await alert(cb, f"Не хватает кредитов: нужно {price}, на счёте {bal}.")
    when = "Код придёт сразу." if stock else "Код пришлю, как только владелец его добавит (обычно в течение суток); до этого заявку можно отменить."
    await reply(cb, f"{title}: обменять {price} кредитов на код активации? Останется {bal - price}.\n{when}",
                kb([[("✅ Да, обменять", f"cr:mok:{kind}"), ("◀️ Назад", "cr:mod")]]))


@router.callback_query(F.data.regexp(r"^cr:mok:\w+$"))
async def cr_mod_ok(cb: CallbackQuery, user: User) -> None:
    kind = cb.data.split(":")[2]
    async with _lock, Session() as s:
        row = await s.get(User, user.tg_id)
        try:
            mc = await credits.buy_mod(s, row, kind)
        except credits.CreditError as e:
            return await alert(cb, str(e))
        await s.commit()
        name = await _mod_bot(s)
    if mc.code:
        return await reply(cb, code_text(mc, name), code_markup(mc, name))
    await reply(cb, "⏳ Заявка принята, кредиты списаны. Код пришлю сюда, как только владелец его добавит.",
                kb([[("↩️ Отменить и вернуть кредиты", f"cr:cancel:{mc.id}")], [("💰 Мои кредиты", "cr:menu")]]))
    title = credits.MOD_KINDS[kind][0]
    for uid in config.OWNER_IDS:
        try:
            await notify._bot.send_message(
                uid, f"🔑 {esc(user.name or str(user.tg_id))} купил за кредиты «{title}», а кодов в запасе нет.\n"
                     "Создайте код в боте модерации (/newcode) и добавьте его — человек получит код автоматически.",
                reply_markup=kb([[("🔑 Добавить коды", f"cra:add:{kind}")]]))
        except Exception:
            pass


@router.callback_query(F.data.regexp(r"^cr:cancel:\d+$"))
async def cr_cancel(cb: CallbackQuery, user: User) -> None:
    async with _lock, Session() as s:
        mc = await s.get(ModCode, int(cb.data.split(":")[2]))
        if mc is None or mc.user_id != user.tg_id:
            return await alert(cb, "Заявка не найдена.")
        try:
            back = await credits.cancel_waiting(s, mc)
        except credits.CreditError as e:
            return await alert(cb, str(e))
        await s.commit()
    await cb.answer(f"Отменено, вернул {back} кредитов", show_alert=True)
    await show(cb.message, user)


# ---------- админ ----------
async def admin_view(target) -> None:
    async with Session() as s:
        total, holders, earned = await credits.totals(s)
        stock = await credits.stock(s)
        people = await credits.top(s)
        prices = {k: await settings.get_int(s, key) for k, (_, key) in credits.MOD_KINDS.items()}
        on = await credits.enabled(s)
    lines = ["💰 <b>Кредиты</b> — 1 кредит = 1 ₽ внутри бота. Начисляются админам групп (комиссия с оплат) и вами; "
             "тратятся на дни 💎 и коды бота модерации. Деньгами не выводятся.",
             f"На счетах: {total} у {holders} чел. · начислено за всё время: {earned}"
             + ("" if on else "\n⏸ Кнопка у людей скрыта: ⚙️ → 💰 Кредиты."), "",
             "🔑 <b>Коды бота модерации</b> (создаёте там: /newcode месяц | год | навсегда):"]
    rows = []
    for kind, (title, _) in credits.MOD_KINDS.items():
        have, wait = stock[kind]
        lines.append(f"• {title.replace('🛡 ', '')}: в запасе {have}" + (f", ⏳ ждут {wait}" if wait else "")
                     + ("" if prices[kind] else " · не продаётся"))
        rows.append([(f"➕ {title.replace('🛡 Бот модерации — ', 'Коды: ')}", f"cra:add:{kind}"),
                     (f"📋 Запас {have}", f"cra:codes:{kind}")])
    waiting = sum(w for _, w in stock.values())
    if people:
        lines += ["", "👥 <b>У кого больше всего</b>:"] + [
            f"• {esc(u.name or 'без имени')} (ID {u.tg_id}) — {u.credits}" for u in people]
    rows.append([(f"⏳ Ждут кода · {waiting}", "cra:wait"), ("✏️ Начислить / списать", "cra:adj")])
    sec = next((i for i, (icon, _, _) in enumerate(settings.SECTIONS) if icon == "💰"), 0)
    rows.append([("⚙️ Цены и включение", f"st:sec:{sec}")])
    await reply(target, "\n".join(lines), kb(rows))


@admin_router.callback_query(F.data == "cra:menu")
async def cra_menu(cb: CallbackQuery) -> None:
    await admin_view(cb)


@admin_router.callback_query(F.data.regexp(r"^cra:add:\w+$"))
async def cra_add(cb: CallbackQuery, state: FSMContext) -> None:
    kind = cb.data.split(":")[2]
    if kind not in credits.MOD_KINDS:
        return await alert(cb, "Нет такого тарифа.")
    await ask(cb, state, "cra_codes", f"🔑 Пришлите коды для «{credits.MOD_KINDS[kind][0]}» — можно несколько, каждый с новой "
                                      "строки. Сначала они уйдут тем, кто ждёт, остальные лягут в запас.", kind=kind)


async def on_codes(s, m: Message, d, text: str):
    """Ввод кодов (inputs.HANDLERS["cra_codes"])."""
    kind = d.get("kind", "")
    if kind not in credits.MOD_KINDS:
        return await m.answer("Нет такого тарифа.")
    if not credits.parse_codes(text):
        return await m.answer("Не нашёл кодов. Код выглядит так: ABCD-EFGH-JKLM.")
    stocked, filled = await credits.add_codes(s, kind, text)
    await s.commit()
    for row in filled:
        await send_code(row)
    await m.answer(f"✅ Выдано ждущим: {len(filled)}, в запас: {stocked}.", reply_markup=kb([[("💰 Кредиты", "cra:menu")]]))


@admin_router.callback_query(F.data.regexp(r"^cra:codes:\w+$"))
async def cra_codes(cb: CallbackQuery) -> None:
    kind = cb.data.split(":")[2]
    async with Session() as s:
        rows = await credits.unsold(s, kind)
    title = credits.MOD_KINDS.get(kind, (kind,))[0]
    if not rows:
        return await reply(cb, f"{title}: в запасе кодов нет.", kb([[("➕ Добавить", f"cra:add:{kind}"), ("◀️ Назад", "cra:menu")]]))
    btns = [[(f"🗑 {r.code}", f"cra:del:{r.id}")] for r in rows[:20]]
    await reply(cb, f"{title}: в запасе {len(rows)}. Нажмите код, чтобы удалить его из запаса "
                    "(например, если он уже использован или отозван в боте модерации).",
                kb(btns + [[("◀️ Назад", "cra:menu")]]))


@admin_router.callback_query(F.data.regexp(r"^cra:del:\d+$"))
async def cra_del(cb: CallbackQuery) -> None:
    async with Session() as s:
        row = await s.get(ModCode, int(cb.data.split(":")[2]))
        if row is None or row.user_id:
            return await alert(cb, "Код уже выдан или удалён.")
        kind = row.kind
        await s.delete(row)
        await s.commit()
    await cb.answer("Удалён из запаса")
    cb.data = f"cra:codes:{kind}"
    await cra_codes(cb)


@admin_router.callback_query(F.data == "cra:wait")
async def cra_wait(cb: CallbackQuery) -> None:
    async with Session() as s:
        rows = await credits.waiting_of(s)
        users = {r.user_id: await s.get(User, r.user_id) for r in rows}
    if not rows:
        return await reply(cb, "⏳ Никто не ждёт кода.", kb([[("◀️ Назад", "cra:menu")]]))
    lines = ["⏳ <b>Ждут кода бота модерации</b> — добавьте коды кнопкой «➕», они уйдут сами. Или отмените с возвратом кредитов."]
    btns = []
    for r in rows[:20]:
        u = users.get(r.user_id)
        lines.append(f"• {esc(u.name if u else str(r.user_id))} — {credits.MOD_KINDS.get(r.kind, (r.kind,))[0]} ({r.price} кр.)")
        btns.append([(f"↩️ Вернуть {r.price} кр. — {(u.name if u else r.user_id)}"[:60], f"cra:ret:{r.id}")])
    await reply(cb, "\n".join(lines), kb(btns + [[("◀️ Назад", "cra:menu")]]))


@admin_router.callback_query(F.data.regexp(r"^cra:ret:\d+$"))
async def cra_ret(cb: CallbackQuery) -> None:
    async with _lock, Session() as s:
        row = await s.get(ModCode, int(cb.data.split(":")[2]))
        if row is None:
            return await alert(cb, "Заявка не найдена.")
        uid = row.user_id
        try:
            back = await credits.cancel_waiting(s, row)
        except credits.CreditError as e:
            return await alert(cb, str(e))
        await s.commit()
    try:
        await notify._bot.send_message(uid, f"↩️ Заявку на код бота модерации отменили — {back} кредитов вернулись на счёт.",
                                       reply_markup=kb([[("💰 Мои кредиты", "cr:menu")]]))
    except Exception:
        pass
    await cb.answer("Отменено, кредиты возвращены")
    await cra_wait(cb)


@admin_router.callback_query(F.data == "cra:adj")
async def cra_adj(cb: CallbackQuery, state: FSMContext) -> None:
    await ask(cb, state, "cra_adj", "✏️ Пришлите: <b>ID сумма причина</b>\nНапример: <code>123456789 500 бонус за группу</code>\n"
                                    "Списать — со знаком минус: <code>123456789 -200 ошибка</code>")


async def on_adjust(s, m: Message, d, text: str):
    """Ручное начисление (inputs.HANDLERS["cra_adj"])."""
    parts = text.split(maxsplit=2)
    try:
        uid, amount = int(parts[0]), int(parts[1])
    except (IndexError, ValueError):
        return await m.answer("Формат: ID сумма причина. Например: 123456789 500 бонус")
    if not -1_000_000 <= amount <= 1_000_000 or amount == 0:
        return await m.answer("Сумма — от −1 000 000 до 1 000 000, не ноль.")
    note = parts[2] if len(parts) > 2 else ""
    user = await s.get(User, uid)
    if user is None:
        return await m.answer("Пользователь с таким ID не найден.")
    try:
        await credits.add(s, user, amount, "admin", note)
    except credits.CreditError as e:
        return await m.answer(str(e))
    await s.commit()
    await m.answer(f"✅ {esc(user.name or str(uid))}: {'+' if amount > 0 else ''}{amount}, теперь {user.credits}.",
                   reply_markup=kb([[("💰 Кредиты", "cra:menu")]]))
    if amount > 0:
        try:
            await notify._bot.send_message(uid, f"💰 Вам начислено {amount} кредитов" + (f": {esc(note)}" if note else "")
                                           + f". На счёте: {user.credits}.", reply_markup=kb([[("💰 Мои кредиты", "cr:menu")]]))
        except Exception:
            pass


async def remind_waiting() -> bool:
    """Из board_loop: кто-то ждёт код дольше 3 ч — напомнить владельцу (не чаще раза в 12 ч)."""
    from datetime import timedelta

    from app.models import Setting, now

    async with Session() as s:
        waiting = [w for w in await credits.waiting_of(s) if w.sold_at and w.sold_at < now() - timedelta(hours=3)]
        if not waiting:
            return False
        row = await s.get(Setting, "codes_reminded_at")
        stamp = now().isoformat()
        if row is not None and row.value and row.value > (now() - timedelta(hours=12)).isoformat():
            return False
        if row is None:
            s.add(Setting(key="codes_reminded_at", value=stamp))
        else:
            row.value = stamp
        await s.commit()
    kinds = sorted({w.kind for w in waiting})
    await notify._send(f"🔑 Ждут код бота модерации: {len(waiting)} чел. (кредиты уже списаны). Создайте коды в боте "
                       "модерации (/newcode) и добавьте — уйдут сами.",
                       kb([[(f"➕ {credits.MOD_KINDS[k][0].replace('🛡 Бот модерации — ', 'Коды: ')}", f"cra:add:{k}")
                                         for k in kinds], [("⏳ Кто ждёт", "cra:wait")]]))
    return True
