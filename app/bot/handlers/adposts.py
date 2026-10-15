"""📣 Реклама в группах: заказ пользователем, проверка и управление админом (публикуют разведчики)."""
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select

from app.bot import notify
from app.bot.common import ask, esc, kb, reply
from app.db import Session
from app.models import AdOrder, Group, ScoutAccount, User
from app.services import adposts, payments, settings

router = Router()        # пользователи
admin_router = Router()  # администратор (подключается к admin в setup.py)

FORBIDDEN = ("алкоголь, табак, казино и ставки, займы под проценты, 18+, наркотики, оружие, пирамиды и обман, "
             "политику и экстремизм")


def approved_message(order_id: int, price: int, status: str):
    """Сообщение заказчику после одобрения: платный — сразу с кнопкой «Оплатить», бесплатный — «в очереди»."""
    if status == "approved":
        return (f"✅ Заказ рекламы №{order_id} одобрен. Осталось оплатить {price} ₽ — нажмите кнопку (СБП или карта), "
                "посты встанут в очередь сами.", kb([[(f"💳 Оплатить {price} ₽", f"ap:pay:{order_id}")]]))
    return f"✅ Заказ рекламы №{order_id} одобрен и поставлен в очередь на публикацию.", None


# ---------------------------------------------------------------- пользователь
async def menu_rows(s) -> list[list[tuple[str, str]]]:
    """Кнопка для 🌐 Каталога (только когда сервис включён)."""
    return [[("📣 Реклама моего бизнеса в группах", "ap:menu")]] if await adposts.enabled(s) else []


@router.callback_query(F.data == "ap:menu")
async def ap_menu(cb: CallbackQuery, user: User) -> None:
    await show_menu(cb, user)


@router.message(Command("ads"))
async def ads_cmd(m: Message, user: User) -> None:
    """/ads и ссылка ?start=ads — рекламодатель сразу попадает сюда, без выбора ниш."""
    await link_entry(m, user)


async def link_entry(target, user: User) -> None:
    """Человек пришёл за рекламой: регистрация с нишами ему не нужна — сразу открываем заказ рекламы."""
    async with Session() as s:
        row = await s.get(User, user.tg_id)
        if row is not None:
            if not row.source:
                row.source = "ads"
            if not row.onboarded:
                row.onboarded = True  # без ниш заявок не получает; чистка «пустых» его не удалит
            await s.commit()
    user.onboarded = True
    await show_menu(target, user)


async def show_menu(target, user: User) -> None:
    async with Session() as s:
        if not await adposts.enabled(s):
            text = "Реклама в группах пока не подключена."
            if isinstance(target, CallbackQuery):
                return await target.answer(text, show_alert=True)
            return await target.answer(text)
        groups = len(await adposts.eligible_groups(s))
        mine = await s.scalar(select(func.count()).select_from(AdOrder).where(AdOrder.user_id == user.tg_id, AdOrder.status != "draft"))
    cost = "у каждой группы и канала своя (смотрите в списке)" if groups else "—"
    await reply(target, (
        "📣 <b>Реклама в группах и каналах</b>\n\n"
        "Вы присылаете текст, выбираете группы или каналы — мы публикуем ваше объявление там.\n\n"
        f"💰 Стоимость: {cost}\n👥 Доступно мест: {groups}\n\n"
        f"Не принимаем: {FORBIDDEN}. Каждый заказ проверяет администратор; под постом будет пометка «Реклама»."
    ), kb(([[("➕ Новый заказ", "ap:new")], [("📋 Куда можно разместить (список и цены)", "ap:list")]] if groups else [])
          + [[(f"🧾 Мои заказы ({mine})", "ap:mine")], [("◀️ Назад", "cg:menu")]]))


@router.callback_query(F.data == "ap:list")
async def ap_list(cb: CallbackQuery, user: User) -> None:
    """Список групп и каналов с числом участников и ценой — посмотреть до заказа."""
    async with Session() as s:
        if not await adposts.enabled(s):
            return await cb.answer("Реклама в группах пока не подключена", show_alert=True)
        groups = await adposts.eligible_groups(s, user.city)
        default = await settings.get_int(s, "adpost_price")
    if not groups:
        return await cb.answer("Пока нет групп, доступных для рекламы", show_alert=True)

    def line(g: Group) -> str:
        price = adposts.group_price(g, default)
        name = f"<a href=\"https://t.me/{esc(g.username)}\">{esc(g.title[:44])}</a>" if g.username else esc(g.title[:44])
        return (f"• {KIND.get(g.kind, '')} {name} — {g.members} уч." + (f", {esc(g.city)}" if g.city else "")
                + f" — <b>{price} ₽</b>" if price else f"• {KIND.get(g.kind, '')} {name} — {g.members} уч. — бесплатно")

    total = sum(g.members for g in groups[:30])
    await reply(cb, ("📋 <b>Где можно разместить рекламу</b> (цена за один пост)\n\n" + "\n".join(line(g) for g in groups[:30])
                     + f"\n\nВсего: {len(groups)}, около {total} участников.\nВыберите нужные при оформлении заказа."),
                kb([[("➕ Новый заказ", "ap:new")], [("◀️ Назад", "ap:menu")]]))


@router.callback_query(F.data == "ap:new")
async def ap_new(cb: CallbackQuery, state: FSMContext) -> None:
    await ask(cb, state, "ap_text", (
        f"Пришлите <b>текст рекламы</b> ({adposts.MIN_TEXT}–{adposts.MAX_TEXT} знаков): что предлагаете, цена, как связаться. "
        "Ссылки и @ники можно."))


async def ap_text(s, m: Message, d, text):
    """Текст получен → черновик → выбор групп."""
    try:
        order = await adposts.new_draft(s, m.from_user.id, text)
    except adposts.AdError as e:
        await m.answer(str(e))
        return False if "Текст рекламы" in str(e) else None
    user = await s.get(User, m.from_user.id)
    await show_picker(m, s, order, user.city if user else "")


KIND = {"group": "👥", "channel": "📢"}


async def show_picker(target, s, order: AdOrder, city: str) -> None:
    groups = (await adposts.eligible_groups(s, city))[:20]
    default = await settings.get_int(s, "adpost_price")
    chosen = set(adposts.selected(order))
    rows = [[(("✅ " if g.id in chosen else "▫️ ") + f"{KIND.get(g.kind, '')} {g.title[:26]} · {g.members} · "
              + (f"{adposts.group_price(g, default)} ₽" if adposts.group_price(g, default) else "бесплатно"), f"ap:t:{order.id}:{g.id}")]
            for g in groups]
    rows.append([(f"Готово ({len(chosen)})", f"ap:review:{order.id}")])
    rows.append([("❌ Отмена", f"ap:cancel:{order.id}")])
    await reply(target, f"Выберите, где разместить рекламу (группы 👥 и каналы 📢, до {adposts.MAX_GROUPS}), нажимая на них:", kb(rows))


async def _own(s, cb: CallbackQuery, user: User, order_id: int) -> AdOrder | None:
    order = await s.get(AdOrder, order_id)
    if order is None or order.user_id != user.tg_id:
        await cb.answer("Заказ не найден", show_alert=True)
        return None
    return order


@router.callback_query(F.data.regexp(r"^ap:pick:\d+$"))
async def ap_pick(cb: CallbackQuery, user: User) -> None:
    async with Session() as s:
        order = await _own(s, cb, user, int(cb.data.split(":")[2]))
        if order is None:
            return
        if order.status != "draft":
            return await cb.answer("Заказ уже отправлен", show_alert=True)
        await show_picker(cb, s, order, user.city)


@router.callback_query(F.data.regexp(r"^ap:t:\d+:\d+$"))
async def ap_toggle(cb: CallbackQuery, user: User) -> None:
    _, _, oid, gid = cb.data.split(":")
    async with Session() as s:
        order = await _own(s, cb, user, int(oid))
        if order is None:
            return
        if order.status != "draft":
            return await cb.answer("Заказ уже отправлен", show_alert=True)
        try:
            await adposts.toggle_group(s, order, int(gid))
        except adposts.AdError as e:
            return await cb.answer(str(e), show_alert=True)
        await show_picker(cb, s, order, user.city)


async def summary(s, order: AdOrder, admin: bool = False) -> str:
    posts = await adposts.posts_of(s, order.id)
    if posts:
        lines = [f"{adposts.POST_STATUS[p.status]} {esc(g.title[:40])}" + (f" — {esc(p.error)}" if p.error else "")
                 + (f' (<a href="{p.link}">пост</a>)' if p.link else "") for p, g in posts]
    else:
        groups = (await s.scalars(select(Group).where(Group.id.in_(adposts.selected(order))))).all()
        default = await settings.get_int(s, "adpost_price")
        lines = [f"▫️ {KIND.get(g.kind, '')} {esc(g.title[:40])} — {adposts.group_price(g, default)} ₽" for g in groups]
    who = ""
    if admin:
        u = await s.get(User, order.user_id)
        who = f"👤 {esc((u.name if u else '') or str(order.user_id))}" + (f" @{esc(u.username)}" if u and u.username else "") + "\n"
    price = f"{order.price} ₽" if order.price else "бесплатно"
    return (f"📣 <b>Заказ №{order.id}</b> — {adposts.STATUS[order.status]}\n{who}💰 {price}\n\n"
            f"{esc(order.text[:700])}\n\nГруппы:\n" + "\n".join(lines))


@router.callback_query(F.data.regexp(r"^ap:review:\d+$"))
async def ap_review(cb: CallbackQuery, user: User) -> None:
    async with Session() as s:
        order = await _own(s, cb, user, int(cb.data.split(":")[2]))
        if order is None:
            return
        n = len(adposts.selected(order))
        if not n:
            return await cb.answer("Выберите хотя бы одну группу", show_alert=True)
        total = await adposts.price_of_groups(s, adposts.selected(order))
        text = await summary(s, order)
    cost = f"\n\nИтого: {total} ₽" if total else "\n\nИтого: бесплатно"
    await reply(cb, text + cost + "\n\nОтправить заказ на проверку администратору?",
                kb([[("📨 Отправить на проверку", f"ap:send:{order.id}")], [("✏️ Изменить группы", f"ap:pick:{order.id}")],
                    [("❌ Отмена", f"ap:cancel:{order.id}")]]))


@router.callback_query(F.data.regexp(r"^ap:send:\d+$"))
async def ap_send(cb: CallbackQuery, user: User) -> None:
    async with Session() as s:
        order = await _own(s, cb, user, int(cb.data.split(":")[2]))
        if order is None:
            return
        try:
            await adposts.submit(s, order)
        except adposts.AdError as e:
            return await cb.answer(str(e), show_alert=True)
        auto = await adposts.maybe_auto_approve(s, order)  # чистый заказ — без участия администратора
        text = await summary(s, order, admin=True)
        flags = adposts.suspicious(order.text)
        oid, price = order.id, order.price
    if auto:
        msg, markup = approved_message(oid, price, auto)
        await reply(cb, msg, markup or kb([[("📋 Мои заказы", "ap:mine")]]))
        return
    warn = f"\n\n⚠️ Подозрительные слова: {', '.join(flags)}" if flags else ""
    await notify._send("📣 Новый заказ рекламы:\n\n" + text + warn,
                       kb([[("✅ Одобрить", f"apa:ok:{oid}"), ("❌ Отклонить", f"apa:no:{oid}")]]))
    await reply(cb, f"✅ Заказ №{oid} отправлен на проверку. Напишу, когда администратор решит.",
                kb([[("📋 Мои заказы", "ap:mine")], [("◀️ Назад", "cg:menu")]]))


@router.callback_query(F.data == "ap:mine")
async def ap_mine(cb: CallbackQuery, user: User) -> None:
    async with Session() as s:
        orders = (await s.scalars(select(AdOrder).where(AdOrder.user_id == user.tg_id, AdOrder.status != "draft")
                                  .order_by(AdOrder.id.desc()).limit(15))).all()
    if not orders:
        return await reply(cb, "Заказов пока нет.", kb([[("◀️ Назад", "ap:menu")]]))
    rows = [[(f"№{o.id} · {adposts.STATUS[o.status]} · {o.created_at:%d.%m}", f"ap:o:{o.id}")] for o in orders]
    await reply(cb, "📋 Ваши заказы рекламы:", kb(rows + [[("◀️ Назад", "ap:menu")]]))


@router.callback_query(F.data.regexp(r"^ap:o:\d+$"))
async def ap_open(cb: CallbackQuery, user: User) -> None:
    async with Session() as s:
        order = await _own(s, cb, user, int(cb.data.split(":")[2]))
        if order is None:
            return
        text = await summary(s, order)
    rows = []
    if order.status == "approved":
        rows.append([("💳 Оплатить", f"ap:pay:{order.id}")])
    if order.status in ("pending", "approved"):
        rows.append([("❌ Отменить заказ", f"ap:cancel:{order.id}")])
    await reply(cb, text, kb(rows + [[("◀️ Назад", "ap:mine")]]))


@router.callback_query(F.data.regexp(r"^ap:pay:\d+$"))
async def ap_pay(cb: CallbackQuery, user: User) -> None:
    if not payments.links_enabled():
        return await cb.answer("Оплата по ссылке пока не подключена", show_alert=True)
    me = await cb.bot.me()
    async with Session() as s:
        order = await _own(s, cb, user, int(cb.data.split(":")[2]))
        if order is None:
            return
        if order.status != "approved":
            return await cb.answer("Этот заказ уже не ждёт оплаты", show_alert=True)
        try:
            url = await payments.create_ad_link(s, order, f"https://t.me/{me.username}")
        except payments.RefundError as e:
            return await cb.answer(str(e), show_alert=True)
    await cb.message.answer(f"🔗 Счёт на {order.price} ₽ создан. Оплатите, и вернитесь в бот — посты встанут в очередь сами.",
                            reply_markup=kb([[("Оплатить (СБП / карта)", f"url:{url}")]]))
    await cb.answer()


@router.callback_query(F.data.regexp(r"^ap:cancel:\d+$"))
async def ap_cancel(cb: CallbackQuery, user: User) -> None:
    async with Session() as s:
        order = await _own(s, cb, user, int(cb.data.split(":")[2]))
        if order is None:
            return
        if order.status not in ("draft", "pending", "approved"):
            return await cb.answer("Этот заказ уже нельзя отменить — напишите администратору", show_alert=True)
        await adposts.cancel(s, order)
    await reply(cb, f"🚫 Заказ №{order.id} отменён.", kb([[("◀️ Реклама", "ap:menu")]]))


# ---------------------------------------------------------------- администратор
@admin_router.callback_query(F.data == "apa:list")
async def adm_list(cb: CallbackQuery) -> None:
    async with Session() as s:
        orders = (await s.scalars(select(AdOrder).where(AdOrder.status.in_(adposts.OPEN_STATUSES))
                                  .order_by(AdOrder.id.desc()).limit(20))).all()
        enabled = await adposts.enabled(s)
    rows = [[(f"№{o.id} · {adposts.STATUS[o.status]} · {o.price} ₽", f"apa:o:{o.id}")] for o in orders]
    head = "📣 Заказы рекламы" + ("" if enabled else " (⚠️ сервис выключен: ⚙️ → 🔍 Разведчик)")
    await reply(cb, head + (":" if orders else "\n\nАктивных заказов нет."), kb(rows + [[("◀️ Назад", "gr:menu")]]))


@admin_router.callback_query(F.data.regexp(r"^apa:o:\d+$"))
async def adm_open(cb: CallbackQuery) -> None:
    await show_order(cb, int(cb.data.split(":")[2]))


async def order_buttons(s, order: AdOrder) -> list[list[tuple[str, str]]]:
    failed = await s.scalar(select(func.count()).select_from(adposts.AdPost).where(
        adposts.AdPost.order_id == order.id, adposts.AdPost.status == "failed"))
    rows = []
    if order.status == "pending":
        rows.append([("✅ Одобрить", f"apa:ok:{order.id}"), ("❌ Отклонить", f"apa:no:{order.id}")])
        rows.append([("💰 Изменить цену", f"apa:price:{order.id}")])
    if failed:
        rows.append([(f"🔁 Повторить неудачные ({failed})", f"apa:retry:{order.id}")])
    if order.status in ("approved", "running", "pending"):
        rows.append([("🚫 Отменить заказ (с причиной)", f"apa:cancel:{order.id}")])
    return rows


async def show_order(cb: CallbackQuery, order_id: int) -> None:
    async with Session() as s:
        order = await s.get(AdOrder, order_id)
        if order is None:
            return await cb.answer("Заказ не найден", show_alert=True)
        text = await summary(s, order, admin=True)
        if order.reason:
            text += f"\n\nПричина: {esc(order.reason)}"
        rows = await order_buttons(s, order)
    await reply(cb, text, kb(rows + [[("◀️ К заказам", "apa:list")]]))


@admin_router.callback_query(F.data.regexp(r"^apa:(ok|retry):\d+$"))
async def adm_act(cb: CallbackQuery) -> None:
    _, op, oid = cb.data.split(":")
    async with Session() as s:
        order = await s.get(AdOrder, int(oid))
        if order is None:
            return await cb.answer("Заказ не найден", show_alert=True)
        user_id, markup, msg = order.user_id, None, ""
        try:
            if op == "ok":
                status = await adposts.approve(s, order)
                msg, markup = approved_message(order.id, order.price, status)
            else:
                if not await adposts.retry_failed(s, order):
                    return await cb.answer("Нечего повторять", show_alert=True)
        except adposts.AdError as e:
            return await cb.answer(str(e), show_alert=True)
    if msg:
        try:
            await notify._bot.send_message(user_id, msg, reply_markup=markup)
        except Exception:
            pass
    await cb.answer("Готово")
    await show_order(cb, int(oid))


@admin_router.callback_query(F.data.regexp(r"^apa:(no|cancel|price):\d+$"))
async def adm_ask(cb: CallbackQuery, state: FSMContext) -> None:
    """Отказ и отмена — с причиной (её увидит заказчик); цена — своя для этого заказа."""
    _, op, oid = cb.data.split(":")
    async with Session() as s:
        order = await s.get(AdOrder, int(oid))
    if order is None:
        return await cb.answer("Заказ не найден", show_alert=True)
    if op == "price":
        return await ask(cb, state, "apa_price", f"Итоговая цена заказа №{oid} в ₽ (сейчас {order.price}):", order_id=int(oid))
    what = "отклоняете" if op == "no" else "отменяете"
    await ask(cb, state, "apa_reason", f"Вы {what} заказ №{oid}. Напишите причину (её получит заказчик) или «-», чтобы без причины:",
              order_id=int(oid), op=op)


async def apa_reason(s, m: Message, d, text):
    order = await s.get(AdOrder, d["order_id"])
    if order is None:
        return await m.answer("Заказ не найден")
    reason = "" if text.strip() == "-" else text.strip()
    try:
        if d["op"] == "no":
            await adposts.reject(s, order, reason)
            msg = f"❌ Заказ рекламы №{order.id} отклонён." + (f"\nПричина: {esc(reason)}" if reason else "")
        else:
            await adposts.cancel(s, order, reason)
            msg = (f"🚫 Заказ рекламы №{order.id} отменён администратором." + (f"\nПричина: {esc(reason)}" if reason else "")
                   + ("\nЕсли вы уже платили — напишите, вернём деньги за неопубликованное." if order.price else ""))
    except adposts.AdError as e:
        return await m.answer(str(e))
    try:
        await notify._bot.send_message(order.user_id, msg)
    except Exception:
        pass
    await m.answer(f"✔ Готово. Заказчик получил сообщение.", reply_markup=kb([[("◀️ К заказам", "apa:list")]]))


async def apa_price(s, m: Message, d, text):
    if not text.strip().isdigit():
        await m.answer("Нужно число (рубли), например 1500")
        return False
    order = await s.get(AdOrder, d["order_id"])
    if order is None:
        return await m.answer("Заказ не найден")
    try:
        await adposts.set_order_price(s, order, int(text))
    except adposts.AdError as e:
        return await m.answer(str(e))
    await m.answer(f"✔ Цена заказа №{order.id}: {order.price} ₽", reply_markup=kb([[("Открыть заказ", f"apa:o:{order.id}")]]))


# ---- группы и каналы для рекламы: разрешение, цена, кто публикует
@admin_router.callback_query(F.data == "apg:list")
async def groups_for_ads(cb: CallbackQuery) -> None:
    async with Session() as s:
        groups = (await s.scalars(select(Group).where(Group.status == "joined", Group.username != "")
                                  .order_by(Group.ads_allowed.desc(), Group.members.desc()).limit(30))).all()
        default = await settings.get_int(s, "adpost_price")
    if not groups:
        return await cb.answer("Пока нет подходящих групп и каналов", show_alert=True)
    rows = [[(("✅ " if g.ads_allowed else "⏸ ") + f"{KIND.get(g.kind, '')} {g.title[:28]} · {adposts.group_price(g, default)} ₽", f"apg:o:{g.id}")]
            for g in groups]
    bulk = [[("💰 Одна цена всем", "apg:allprice"), ("📈 Цена по размеру (от–до)", "apg:algo")],
            [("✅ Разрешить везде", "apg:allow:1"), ("⏸ Запретить везде", "apg:allow:0")],
            [("📨 Готовое сообщение клиенту", "apg:share")]]
    await reply(cb, ("📢 Группы и каналы для рекламы. ✅ — разрешено (покупатель видит и может выбрать). "
                     "Нажмите на любую — разрешить/запретить и цена. Кнопки внизу меняют сразу все:"),
                kb(rows + bulk + [[("◀️ Назад", "gr:menu")]]))


async def show_group_card(cb: CallbackQuery, gid: int) -> None:
    async with Session() as s:
        g = await s.get(Group, gid)
        if g is None:
            return await cb.answer("Не найдено", show_alert=True)
        default = await settings.get_int(s, "adpost_price")
        acc_id = g.ad_account_id or g.account_id
        acc = await s.get(ScoutAccount, acc_id) if acc_id else None
    own = f"{g.ad_price} ₽ (своя)" if g.ad_price > 0 else f"{default} ₽ (общая из ⚙️)"
    who = esc(acc.name) if acc else "— не назначен"
    note = ("\n⚠️ Чтобы публиковать в канал, аккаунт-публикатор должен быть админом канала с правом писать посты."
            if g.kind == "channel" else "")
    rows = [[("⏸ Запретить рекламу" if g.ads_allowed else "✅ Разрешить рекламу", f"apg:tog:{g.id}")],
            [("💰 Цена за пост", f"apg:price:{g.id}")]]
    if g.kind == "channel":
        rows.append([("👤 Сменить аккаунт-публикатор", f"apg:acc:{g.id}")])
    await reply(cb, (f"{KIND.get(g.kind, '')} <b>{esc(g.title)}</b> — {g.members} уч.\n"
                     f"Реклама: {'✅ разрешена' if g.ads_allowed else '⏸ запрещена'}\n💰 Цена за пост: {own}\n"
                     f"👤 Публикует: {who}{note}"), kb(rows + [[("◀️ К списку", "apg:list")]]))


@admin_router.callback_query(F.data.regexp(r"^apg:o:\d+$"))
async def group_card(cb: CallbackQuery) -> None:
    await show_group_card(cb, int(cb.data.split(":")[2]))


@admin_router.callback_query(F.data.regexp(r"^apg:tog:\d+$"))
async def groups_toggle(cb: CallbackQuery) -> None:
    gid = int(cb.data.split(":")[2])
    async with Session() as s:
        g = await s.get(Group, gid)
        if g is None:
            return await cb.answer("Группа не найдена", show_alert=True)
        if not g.ads_allowed and not (g.ad_account_id or g.account_id):
            return await cb.answer("Сначала назначьте аккаунт-публикатор (кнопка «👤 Сменить аккаунт-публикатор»)", show_alert=True)
        g.ads_allowed = not g.ads_allowed
        await s.commit()
    await show_group_card(cb, gid)


@admin_router.callback_query(F.data.regexp(r"^apg:price:\d+$"))
async def group_price_ask(cb: CallbackQuery, state: FSMContext) -> None:
    gid = int(cb.data.split(":")[2])
    await ask(cb, state, "apg_price", "Цена за один пост здесь, ₽ (число). «0» — брать общую цену из ⚙️:", gid=gid)


async def apg_price(s, m: Message, d, text):
    if not text.strip().isdigit() or int(text) > 1_000_000:
        await m.answer("Нужно число (рубли), например 500")
        return False
    g = await s.get(Group, d["gid"])
    if g is None:
        return await m.answer("Группа не найдена")
    g.ad_price = int(text)
    await s.commit()
    await m.answer(f"✔ Цена за пост в «{esc(g.title)}»: {g.ad_price or 'общая'} ₽", reply_markup=kb([[("Открыть", f"apg:o:{g.id}")]]))


@admin_router.callback_query(F.data.regexp(r"^apg:acc:\d+$"))
async def group_account_cycle(cb: CallbackQuery) -> None:
    """Следующий включённый аккаунт-разведчик (для каналов: тот, кого вы сделали админом канала)."""
    gid = int(cb.data.split(":")[2])
    async with Session() as s:
        g = await s.get(Group, gid)
        accs = (await s.scalars(select(ScoutAccount).where(ScoutAccount.enabled, ScoutAccount.can_post).order_by(ScoutAccount.id))).all()
        if g is None or not accs:
            return await cb.answer("Нет подходящих аккаунтов-разведчиков", show_alert=True)
        ids = [a.id for a in accs]
        cur = g.ad_account_id or g.account_id
        g.ad_account_id = ids[(ids.index(cur) + 1) % len(ids)] if cur in ids else ids[0]
        await s.commit()
    await show_group_card(cb, gid)


@admin_router.callback_query(F.data == "apg:share")
async def share_text(cb: CallbackQuery) -> None:
    """Готовое сообщение рекламодателю: список мест с ценами и ссылка, которая сразу открывает заказ в боте."""
    me = await cb.bot.me()
    async with Session() as s:
        groups = await adposts.eligible_groups(s)
        default = await settings.get_int(s, "adpost_price")
    if not groups:
        return await cb.answer("Пока нет разрешённых групп и каналов", show_alert=True)
    lines = [f"• {g.title[:40]} — {g.members} уч. — {adposts.group_price(g, default)} ₽" for g in groups[:15]]
    text = ("Реклама в наших группах и каналах (цена за один пост):\n\n" + "\n".join(lines)
            + f"\n\nВсего {len(groups)} мест, около {sum(g.members for g in groups)} участников.\n\n"
            f"Чтобы заказать: откройте https://t.me/{me.username}?start=ads, пришлите текст и выберите, где разместить. "
            "После проверки придёт кнопка оплаты (СБП или карта), пост выйдет сам. "
            "Для вакансий укажите название компании и телефон или адрес.")
    await cb.message.answer(text)
    await cb.message.answer("☝️ Скопируйте это сообщение и отправьте клиенту (нажмите и удерживайте текст).",
                            reply_markup=kb([[("📢 К списку", "apg:list")]]))
    await cb.answer()


@admin_router.callback_query(F.data == "apg:allprice")
async def all_price_ask(cb: CallbackQuery, state: FSMContext) -> None:
    async with Session() as s:
        n = len(await adposts.candidates(s))
    await ask(cb, state, "apg_allprice", f"Одна цена за пост для ВСЕХ ({n} групп и каналов), ₽. «0» — вернуть общую цену из ⚙️:")


async def apg_allprice(s, m: Message, d, text):
    if not text.strip().isdigit() or int(text) > 1_000_000:
        await m.answer("Нужно число (рубли), например 500")
        return False
    n = await adposts.set_all_prices(s, int(text))
    await m.answer(f"✔ Цена {int(text) or 'общая'} ₽ поставлена в {n} группах и каналах.", reply_markup=kb([[("📢 К списку", "apg:list")]]))


@admin_router.callback_query(F.data == "apg:algo")
async def algo_ask(cb: CallbackQuery, state: FSMContext) -> None:
    async with Session() as s:
        groups = await adposts.candidates(s)
    if not groups:
        return await cb.answer("Нет групп и каналов", show_alert=True)
    await ask(cb, state, "apg_algo", (
        f"📈 Цена по размеру для {len(groups)} групп и каналов. Самая маленькая ({min(g.members for g in groups)} уч.) получит "
        f"нижнюю цену, самая большая ({max(g.members for g in groups)} уч.) — верхнюю, остальные пропорционально участникам.\n\n"
        "Напишите «от-до» в рублях, например: 300-2000"))


async def apg_algo(s, m: Message, d, text):
    rng = adposts.parse_range(text)
    if rng is None:
        await m.answer("Напишите два числа: от и до, например 300-2000 (от не больше до)")
        return False
    low, high = rng
    groups = await adposts.candidates(s)
    prices = adposts.scaled_prices(groups, low, high)
    sample = [groups[0], groups[len(groups) // 2], groups[-1]] if len(groups) > 2 else groups
    lines = [f"• {esc(g.title[:30])} — {g.members} уч. → {prices[g.id]} ₽" for g in sample]
    await m.answer(f"Так будут выставлены цены ({len(groups)} шт.): от {low} до {high} ₽\n\n" + "\n".join(lines)
                   + "\n\nЦены у всех групп и каналов заменятся. Применить?",
                   reply_markup=kb([[("✅ Применить", f"apg:algo_ok:{low}:{high}")], [("Отмена", "apg:list")]]))


@admin_router.callback_query(F.data.regexp(r"^apg:algo_ok:\d+:\d+$"))
async def algo_apply(cb: CallbackQuery) -> None:
    _, _, low, high = cb.data.split(":")
    async with Session() as s:
        n = await adposts.apply_scaled(s, int(low), int(high))
    await cb.answer(f"Цены обновлены: {n} шт.", show_alert=True)
    await groups_for_ads(cb)


@admin_router.callback_query(F.data.regexp(r"^apg:allow:[01]$"))
async def allow_all_handler(cb: CallbackQuery) -> None:
    flag = cb.data.endswith("1")
    async with Session() as s:
        changed, skipped = await adposts.allow_all(s, flag)
    note = f"\nПропущено без аккаунта-публикатора: {skipped} (назначьте его в карточке)." if skipped else ""
    await cb.answer(("Разрешено" if flag else "Запрещено") + f": {changed} шт." + note, show_alert=True)
    await groups_for_ads(cb)


async def counts() -> tuple[int, int]:
    """(открытых заказов, групп и каналов, разрешённых для рекламы) — для меню 🔍 Группы."""
    async with Session() as s:
        orders = await s.scalar(select(func.count()).select_from(AdOrder).where(AdOrder.status.in_(adposts.OPEN_STATUSES))) or 0
        allowed = await s.scalar(select(func.count()).select_from(Group).where(Group.ads_allowed)) or 0
    return orders, allowed
