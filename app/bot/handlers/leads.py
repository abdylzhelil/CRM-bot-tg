from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select

from app.bot import notify
from app.bot.common import ask, esc, kb
from app.db import Session
from app.models import BlacklistEntry, Category, Client, ClientEvent, Delivery, Group, Lead, SpamVote, User, now
from app.services import referrals, settings, templates
from app.services.distribute import day_start

router = Router()
STATUS_NOTES = {"later": ("postponed", "Отложено"), "reject": ("rejected", "Отказ")}


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


async def _blacklist(s, lead: Lead, status: str, reason: str) -> None:
    entry = await s.get(BlacklistEntry, lead.author_id)
    if entry:
        entry.status, entry.reason = status, reason
    else:
        s.add(BlacklistEntry(user_id=lead.author_id, username=lead.username, status=status, reason=reason))


async def vote_spam(s, lead: Lead, user: User) -> None:
    """Голос «спам»: пользователю автор больше не приходит; админ или N голосов — общий чёрный список."""
    if await s.get(SpamVote, (lead.author_id, user.tg_id)) is None:
        s.add(SpamVote(author_id=lead.author_id, user_id=user.tg_id))
        await s.flush()
    votes = await s.scalar(select(func.count()).select_from(SpamVote).where(SpamVote.author_id == lead.author_id))
    if user.is_admin or votes >= await settings.get_int(s, "spam_votes_to_ban"):
        await _blacklist(s, lead, "spam", "отмечено администратором" if user.is_admin else f"голосов «спам»: {votes}")


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
        if action == "take":
            client = await s.scalar(select(Client).where(Client.user_id == user.tg_id, Client.tg_id == lead.author_id))
            if not client:
                group = await s.get(Group, lead.group_id) if lead.group_id else None
                client = Client(
                    user_id=user.tg_id, tg_id=lead.author_id, name=lead.name, username=lead.username,
                    source=group.title if group else lead.group_title, category_id=lead.category_id,
                )
                s.add(client)
                await s.flush()
            s.add(ClientEvent(client_id=client.id, text=f"Заявка: {lead.text[:300]}"))
            d.status = "taken"
            await s.commit()
            await cb.message.edit_reply_markup(reply_markup=kb([[("👤 Открыть клиента", f"cl:open:{client.id}")]]))
            return await cb.answer("Клиент в CRM")
        if action in STATUS_NOTES:
            d.status, note = STATUS_NOTES[action]
        elif action == "spam":
            d.status, note = "spam", "Это спам — автор больше не придёт"
            await vote_spam(s, lead, user)
        elif action == "client" and user.is_admin:
            await _blacklist(s, lead, "client", "отмечено администратором")
            lead.suspicious, note = False, "В белом списке"
        else:
            return await cb.answer()
        await s.commit()
    await cb.message.edit_reply_markup(reply_markup=kb([[(f"✔ {note}", "noop")]]))
    await cb.answer(note)


@router.callback_query(F.data == "noop")
async def noop(cb: CallbackQuery) -> None:
    await cb.answer()


async def send_template(cb: CallbackQuery, s, lead: Lead, user: User) -> None:
    """Ответ клиенту: шаблон и цены пользователя; у администратора — ещё и из категории."""
    cat = await s.get(Category, lead.category_id) if lead.category_id else None
    parent = await s.get(Category, cat.parent_id) if cat and cat.parent_id else None
    tpl, prices = user.reply_template, user.prices
    if user.is_admin:
        tpl = tpl or (cat.reply_template if cat else "") or (parent.reply_template if parent else "")
        prices = prices or (cat.prices if cat else "") or (parent.prices if parent else "")
    text = templates.render(
        tpl, name=lead.name, group=lead.group_title,
        category=(cat.name.lower() if cat else "эта услуга"), prices=prices,
    )
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
    colleague = await s.get(User, int(raw)) if raw.isdigit() else await s.scalar(
        select(User).where(User.username.ilike(raw)))
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
