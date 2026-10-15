"""📣 Реклама в группах: заказ пользователем, проверка и управление админом (публикуют разведчики)."""
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select

from app.bot import notify
from app.bot.common import ask, esc, kb, reply
from app.db import Session
from app.models import AdOrder, Group, User
from app.services import adposts, payments, settings

router = Router()        # пользователи
admin_router = Router()  # администратор (подключается к admin в setup.py)

FORBIDDEN = ("алкоголь, табак, казино и ставки, займы под проценты, 18+, наркотики, оружие, пирамиды и обман, "
             "политику и экстремизм")


# ---------------------------------------------------------------- пользователь
async def menu_rows(s) -> list[list[tuple[str, str]]]:
    """Кнопка для 🌐 Каталога (только когда сервис включён)."""
    return [[("📣 Реклама в группах", "ap:menu")]] if await adposts.enabled(s) else []


@router.callback_query(F.data == "ap:menu")
async def ap_menu(cb: CallbackQuery, user: User) -> None:
    async with Session() as s:
        if not await adposts.enabled(s):
            return await cb.answer("Реклама в группах пока не подключена", show_alert=True)
        price = await settings.get_int(s, "adpost_price")
        groups = len(await adposts.eligible_groups(s))
        mine = await s.scalar(select(func.count()).select_from(AdOrder).where(AdOrder.user_id == user.tg_id, AdOrder.status != "draft"))
    cost = f"{price} ₽ за одну группу" if price else "бесплатно (после проверки администратором)"
    await reply(cb, (
        "📣 <b>Реклама в группах</b>\n\n"
        "Вы присылаете текст, выбираете группы — мы публикуем ваше объявление в этих группах.\n\n"
        f"💰 Стоимость: {cost}\n👥 Доступно групп: {groups}\n\n"
        f"Не принимаем: {FORBIDDEN}. Каждый заказ проверяет администратор; под постом будет пометка «Реклама»."
    ), kb(([[("➕ Новый заказ", "ap:new")]] if groups else []) + [[(f"📋 Мои заказы ({mine})", "ap:mine")], [("◀️ Назад", "cg:menu")]]))


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


async def show_picker(target, s, order: AdOrder, city: str) -> None:
    groups = (await adposts.eligible_groups(s, city))[:20]
    chosen = set(adposts.selected(order))
    rows = [[(("✅ " if g.id in chosen else "▫️ ") + f"{g.title[:34]} · {g.members}", f"ap:t:{order.id}:{g.id}")] for g in groups]
    rows.append([(f"Готово ({len(chosen)})", f"ap:review:{order.id}")])
    rows.append([("❌ Отмена", f"ap:cancel:{order.id}")])
    await reply(target, f"Выберите группы (до {adposts.MAX_GROUPS}), нажимая на них:", kb(rows))


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
        lines = [f"▫️ {esc(g.title[:40])}" for g in groups]
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
        per = await settings.get_int(s, "adpost_price")
        text = await summary(s, order)
    cost = f"\n\nИтого: {per * n} ₽ ({n} × {per})" if per else "\n\nИтого: бесплатно"
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
        text = await summary(s, order, admin=True)
    await notify._send("📣 Новый заказ рекламы:\n\n" + text, kb([[("✅ Одобрить", f"apa:ok:{order.id}"), ("❌ Отклонить", f"apa:no:{order.id}")]]))
    await reply(cb, f"✅ Заказ №{order.id} отправлен на проверку. Напишу, когда администратор решит.",
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


async def show_order(cb: CallbackQuery, order_id: int) -> None:
    async with Session() as s:
        order = await s.get(AdOrder, order_id)
        if order is None:
            return await cb.answer("Заказ не найден", show_alert=True)
        text = await summary(s, order, admin=True)
        failed = await s.scalar(select(func.count()).select_from(adposts.AdPost).where(
            adposts.AdPost.order_id == order.id, adposts.AdPost.status == "failed"))
    rows = []
    if order.status == "pending":
        rows.append([("✅ Одобрить", f"apa:ok:{order.id}"), ("❌ Отклонить", f"apa:no:{order.id}")])
    if failed:
        rows.append([(f"🔁 Повторить неудачные ({failed})", f"apa:retry:{order.id}")])
    if order.status in ("approved", "running", "pending"):
        rows.append([("🚫 Отменить заказ", f"apa:cancel:{order.id}")])
    await reply(cb, text, kb(rows + [[("◀️ К заказам", "apa:list")]]))


@admin_router.callback_query(F.data.regexp(r"^apa:(ok|no|cancel|retry):\d+$"))
async def adm_act(cb: CallbackQuery) -> None:
    _, op, oid = cb.data.split(":")
    async with Session() as s:
        order = await s.get(AdOrder, int(oid))
        if order is None:
            return await cb.answer("Заказ не найден", show_alert=True)
        user_id = order.user_id
        try:
            if op == "ok":
                status = await adposts.approve(s, order)
                msg = (f"✅ Заказ рекламы №{order.id} одобрен. Оплатите {order.price} ₽: 📋 Мои заказы → заказ → «💳 Оплатить»."
                       if status == "approved" else f"✅ Заказ рекламы №{order.id} одобрен и поставлен в очередь на публикацию.")
            elif op == "no":
                await adposts.reject(s, order)
                msg = f"❌ Заказ рекламы №{order.id} отклонён администратором."
            elif op == "cancel":
                n = await adposts.cancel(s, order)
                msg = (f"🚫 Заказ рекламы №{order.id} отменён администратором. Если вы уже платили — напишите, вернём деньги за "
                       "неопубликованное.") if order.price else f"🚫 Заказ рекламы №{order.id} отменён администратором."
            else:
                n = await adposts.retry_failed(s, order)
                msg = ""
                if not n:
                    return await cb.answer("Нечего повторять", show_alert=True)
        except adposts.AdError as e:
            return await cb.answer(str(e), show_alert=True)
    if msg:
        try:
            await notify._bot.send_message(user_id, msg)
        except Exception:
            pass
    await show_order(cb, int(oid))


@admin_router.callback_query(F.data == "apg:list")
async def groups_for_ads(cb: CallbackQuery) -> None:
    async with Session() as s:
        groups = (await s.scalars(select(Group).where(Group.status == "joined", Group.kind == "group", Group.account_id.is_not(None))
                                  .order_by(Group.ads_allowed.desc(), Group.members.desc()).limit(25))).all()
    if not groups:
        return await cb.answer("Разведчик пока ни в одной группе", show_alert=True)
    rows = [[(("✅ " if g.ads_allowed else "⏸ ") + f"{g.title[:36]} · {g.members}", f"apg:tog:{g.id}")] for g in groups]
    await reply(cb, ("📢 Группы для рекламы. ✅ — разведчик может публиковать там рекламные посты (нажмите, чтобы переключить). "
                     "Разрешайте только там, где реклама допустима по правилам группы:"), kb(rows + [[("◀️ Назад", "gr:menu")]]))


@admin_router.callback_query(F.data.regexp(r"^apg:tog:\d+$"))
async def groups_toggle(cb: CallbackQuery) -> None:
    async with Session() as s:
        g = await s.get(Group, int(cb.data.split(":")[2]))
        if g is None:
            return await cb.answer("Группа не найдена", show_alert=True)
        g.ads_allowed = not g.ads_allowed
        await s.commit()
    await groups_for_ads(cb)


async def counts() -> tuple[int, int]:
    """(открытых заказов, групп, разрешённых для рекламы) — для меню 🔍 Группы."""
    async with Session() as s:
        orders = await s.scalar(select(func.count()).select_from(AdOrder).where(AdOrder.status.in_(adposts.OPEN_STATUSES))) or 0
        allowed = await s.scalar(select(func.count()).select_from(Group).where(Group.ads_allowed)) or 0
    return orders, allowed
