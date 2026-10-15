from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select

from app.bot import notify
from app.bot.common import ask, esc, kb
from app.db import Session
from app.models import Category, Delivery, Lead, User, now
from app.services import ai, draft, lead_actions, referrals, settings
from app.services.distribute import day_start, root_category

router = Router()
STATUS_NOTES = lead_actions.STATUS_NOTES
_blacklist = lead_actions.blacklist
vote_spam = lead_actions.vote_spam


async def category_name(s, cat_id: int | None) -> str:
    cat = await s.get(Category, cat_id) if cat_id else None
    if not cat:
        return "—"
    parent = await s.get(Category, cat.parent_id) if cat.parent_id else None
    return f"{(parent or cat).emoji} {parent.name + ' → ' if parent else ''}{cat.name}".strip()


async def send_card(msg: Message, s, d: Delivery, lead: Lead, user: User) -> None:
    await msg.answer(
        notify.lead_text(lead, await category_name(s, lead.category_id)),
        reply_markup=notify.lead_keyboard(lead, d.id, user.is_admin), disable_web_page_preview=True,
    )


@router.message(F.text == "📥 Заявки")
async def leads_menu(m: Message, user: User) -> None:
    async with Session() as s:
        mine = Delivery.user_id == user.tg_id
        cnt = dict((await s.execute(
            select(Delivery.status, func.count()).where(mine, Delivery.sent).group_by(Delivery.status)
        )).all())
        waiting = await s.scalar(select(func.count()).select_from(Delivery).where(mine, ~Delivery.sent))
        rows = (await s.execute(
            select(Delivery, Lead).join(Lead, Lead.id == Delivery.lead_id)
            .where(mine, Delivery.sent, Delivery.status.in_(["new", "postponed"]))
            .order_by(Delivery.id.desc()).limit(5)
        )).all()
        await m.answer(
            f"📥 Заявки: новых {cnt.get('new', 0)}, отложено {cnt.get('postponed', 0)}, "
            f"в работе {cnt.get('taken', 0)}, отказ {cnt.get('rejected', 0)}"
            + (f"\n⏳ Ещё придут: {waiting}" if waiting else "")
            + ("\nПоследние:" if rows else "\nНовых заявок пока нет — пришлю, как только появятся.")
        )
        for d, lead in rows:
            await send_card(m, s, d, lead, user)


@router.callback_query(F.data.startswith("lead:"))
async def lead_action(cb: CallbackQuery, user: User, state: FSMContext = None) -> None:
    _, action, raw_id = cb.data.split(":")
    async with Session() as s:
        d = await s.get(Delivery, int(raw_id))
        lead = await s.get(Lead, d.lead_id) if d else None
        if not d or not lead or d.user_id != user.tg_id:
            return await cb.answer("Заявка не найдена", show_alert=True)
        if action == "pass":
            return await ask(cb, state, "pass_to", (
                "📨 Кому передать заявку? Пришлите Telegram ID или @username коллеги.\n"
                "Если его ещё нет в боте — дам ссылку-приглашение (вы оба получите бонус)."), did=d.id)
        if action == "tpl":
            return await send_template(cb, s, lead, user)
        if action == "ai":
            return await send_ai_draft(cb, s, lead, user, d.id)
        if action == "take":
            client = await lead_actions.take(s, user, d, lead)
            await s.commit()
            await cb.message.edit_reply_markup(reply_markup=kb([[("👤 Открыть клиента", f"cl:open:{client.id}")]]))
            return await cb.answer("Клиент в CRM")
        try:
            note = await lead_actions.apply_status(s, user, d, lead, action)
        except lead_actions.ActionError:
            return await cb.answer()
        await s.commit()
    await cb.message.edit_reply_markup(reply_markup=kb([[(f"✔ {note}", "noop")]]))
    await cb.answer(note)


@router.callback_query(F.data == "noop")
async def noop(cb: CallbackQuery) -> None:
    await cb.answer()


async def send_template(cb: CallbackQuery, s, lead: Lead, user: User) -> None:
    """Ответ клиенту: шаблон и цены пользователя; у администратора — ещё и из категории."""
    text = await lead_actions.template_text(s, user, lead)
    contact = f"https://t.me/{lead.username}" if lead.username else f"tg://user?id={lead.author_id}"
    await cb.message.answer(
        f"📋 Ответ клиенту (нажмите на текст — скопируется):\n\n<code>{esc(text)}</code>\n\n"
        "<i>Свой шаблон и цены: 👤 Профиль → 📋 Мой шаблон / 💰 Мои цены.</i>",
        reply_markup=kb([[("✍️ Написать клиенту", f"url:{contact}")]]),
    )
    await cb.answer()


async def pass_to(s, m: Message, d, text):
    """Передать заявку коллеге из бота; если его нет — ссылка-приглашение."""
    mine = await s.get(Delivery, d["did"])
    if mine is None or mine.user_id != m.from_user.id:
        return await m.answer("Заявка не найдена.")
    me = await s.get(User, m.from_user.id)
    raw = text.strip().lstrip("@")
    if not raw:
        return await m.answer("Напишите @ник или ID коллеги.")
    # точное совпадение ника (без учёта регистра); ilike принял бы «%» как «кто угодно»
    colleague = await s.get(User, int(raw)) if raw.isascii() and raw.isdigit() and len(raw) < 19 else await s.scalar(
        select(User).where(func.lower(User.username) == raw.lower()).limit(1))
    if colleague is None or not colleague.onboarded:
        bot_name = (await m.bot.me()).username
        return await m.answer(
            "Этого человека ещё нет в боте. Отправьте ему приглашение — вы оба получите "
            f"по {await settings.get_int(s, 'ref_days')} дней 💎:\n{referrals.link(bot_name, me.tg_id)}")
    if colleague.tg_id == me.tg_id:
        return await m.answer("Это вы сами 🙂")
    if await s.scalar(select(Delivery.id).where(Delivery.lead_id == mine.lead_id, Delivery.user_id == colleague.tg_id)):
        return await m.answer("У коллеги уже есть эта заявка.")
    if not me.is_admin:
        today = await s.scalar(select(func.count()).select_from(Delivery).where(
            Delivery.from_user == me.tg_id, Delivery.created_at >= day_start()))
        if today >= await settings.get_int(s, "pass_daily_limit"):
            return await m.answer("На сегодня лимит передач исчерпан.")
    s.add(Delivery(lead_id=mine.lead_id, user_id=colleague.tg_id, from_user=me.tg_id, deliver_after=now()))
    await s.commit()
    await notify.flush_due()
    await m.answer(f"✅ Заявка передана: {esc(colleague.name or str(colleague.tg_id))}")


async def send_ai_draft(cb: CallbackQuery, s, lead: Lead, user: User, delivery_id: int, client=None) -> None:
    """✨ Черновик ответа: простой по правилам (бесплатно) или от Claude, если владелец включил (ai_enabled=1 и ключ в .env)."""
    contact = f"https://t.me/{lead.username}" if lead.username else f"tg://user?id={lead.author_id}"
    use_claude = ai.enabled() and await settings.get(s, "ai_enabled") == "1"
    if use_claude:
        await cb.answer("✨ Пишу черновик…")
        try:
            text = await ai.draft_reply(s, user, lead, await category_name(s, lead.category_id),
                                        user.reply_template, user.prices, client=client)
        except ai.AiError as e:
            return await cb.message.answer(f"✨ {esc(str(e))}")
    else:
        root_id = await root_category(s, lead.category_id)
        root = (await s.get(Category, root_id)).name if root_id else ""
        text = draft.build(lead.text, lead.name, root, user.reply_template, user.prices)
        await cb.answer()
    await cb.message.answer(
        f"✨ <b>Черновик ответа</b> (нажмите на текст — скопируется, поправьте под себя):\n\n<code>{esc(text)}</code>",
        reply_markup=kb([[("🔄 Другой вариант", f"lead:ai:{delivery_id}"), ("✍️ Написать клиенту", f"url:{contact}")]]))
