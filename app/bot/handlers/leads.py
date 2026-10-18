from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select

from app.bot import notify
from app.bot.common import COPY_MAX, ask, esc, kb
from app.db import Session
from app.models import Category, Client, Delivery, Group, Lead, User, now
from app.services import ai, draft, lead_actions, mute, referrals, settings
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


LISTS = (("new", "🆕 Новые"), ("postponed", "⏰ Отложенные"), ("taken", "✅ В работе"),
         ("rejected", "🙈 Не моё"), ("spam", "🚫 Спам"))
PAGE = 5


async def status_counts(s, user_id: int) -> dict:
    return dict((await s.execute(
        select(Delivery.status, func.count()).where(Delivery.user_id == user_id, Delivery.sent).group_by(Delivery.status)
    )).all())


def lists_keyboard(cnt: dict):
    rows = [[(f"{label} · {cnt.get(st, 0)}", f"ll:{st}:0")] for st, label in LISTS]
    return kb([rows[0] + rows[1], rows[2] + rows[3], rows[4]])


def card_keyboard(lead: Lead, d: Delivery, user: User, client_id: int | None = None):
    """Кнопки под карточкой в зависимости от статуса: из «Не моё» и «Спама» можно вернуть."""
    if d.status in ("new", "postponed"):
        return notify.lead_keyboard(lead, d.id, user.is_admin)
    if d.status == "taken":
        return kb([[("👤 Открыть клиента", f"cl:open:{client_id}")]]) if client_id else None
    rows = [[("✍️ Написать", f"url:{lead_actions.contact_url(lead)}"), ("↩️ Вернуть в новые", f"lead:restore:{d.id}")]]
    if d.status == "rejected":
        rows.append([("🙅 Исполнитель (научить фильтр)", f"lead:offer:{d.id}")])
    return kb(rows)


async def send_card(msg: Message, s, d: Delivery, lead: Lead, user: User) -> None:
    client_id = None
    if d.status == "taken":
        client_id = await s.scalar(select(Client.id).where(Client.user_id == user.tg_id, Client.tg_id == lead.author_id))
    await msg.answer(
        notify.lead_text(lead, await category_name(s, lead.category_id)),
        reply_markup=card_keyboard(lead, d, user, client_id), disable_web_page_preview=True,
    )


@router.message(F.text == "📥 Заявки")
async def leads_menu(m: Message, user: User) -> None:
    async with Session() as s:
        mine = Delivery.user_id == user.tg_id
        cnt = await status_counts(s, user.tg_id)
        waiting = await s.scalar(select(func.count()).select_from(Delivery).where(mine, ~Delivery.sent))
        rows = (await s.execute(
            select(Delivery, Lead).join(Lead, Lead.id == Delivery.lead_id)
            .where(mine, Delivery.sent, Delivery.status.in_(["new", "postponed"]))
            .order_by(Delivery.id.desc()).limit(PAGE)
        )).all()
        row = await s.get(User, user.tg_id)
        muted_n = await mute.accumulated(s, user.tg_id)
        markup = lists_keyboard(cnt)
        if muted_n:
            markup.inline_keyboard.append(kb([[(f"📥 Накопилось без уведомлений · {muted_n}", "ll:muted:0")]]).inline_keyboard[0])
        markup.inline_keyboard.append(kb([[(f"🔕 Уведомления выключены {mute.until_text(row)} — включить" if mute.is_muted(row)
                                            else "🔕 Выключить уведомления на время", "mt:menu")]]).inline_keyboard[0])
        await m.answer(
            "📥 Заявки по спискам — нажмите, чтобы открыть:"
            + (f"\n⏳ Ещё придут: {waiting}" if waiting else "")
            + (f"\n🔕 Уведомления выключены {mute.until_text(row)} — заявки копятся без сообщений." if mute.is_muted(row) else "")
            + ("\nПоследние новые и отложенные ниже." if rows else "\nНовых заявок пока нет — пришлю, как только появятся."),
            reply_markup=markup,
        )
        for d, lead in rows:
            await send_card(m, s, d, lead, user)


@router.callback_query(F.data.regexp(r"^ll:(new|postponed|taken|rejected|spam|muted):\d{1,18}$"))
async def leads_list(cb: CallbackQuery, user: User) -> None:
    """Список заявок одного статуса, по 5, от новых к старым; «Ещё» — следующая страница."""
    _, status, raw = cb.data.split(":")
    before = int(raw)
    async with Session() as s:
        q = (select(Delivery, Lead).join(Lead, Lead.id == Delivery.lead_id)
             .where(Delivery.user_id == user.tg_id, Delivery.sent))
        # «🔕 накопилось»: пришли без уведомления и ещё не разобраны (они же есть в «🆕 Новые»)
        q = q.where(Delivery.muted, Delivery.status == "new") if status == "muted" else q.where(Delivery.status == status)
        if before:
            q = q.where(Delivery.id < before)
        rows = (await s.execute(q.order_by(Delivery.id.desc()).limit(PAGE + 1))).all()
        label = dict(LISTS).get(status, "📥 Накопилось без уведомлений")
        if not rows:
            await cb.answer()
            return await cb.message.answer(f"{label}: пусто.")
        await cb.message.answer(f"{label}:" if not before else f"{label} (дальше):")
        for d, lead in rows[:PAGE]:
            await send_card(cb.message, s, d, lead, user)
    if len(rows) > PAGE:
        await cb.message.answer("Есть ещё.", reply_markup=kb([[("⬇️ Показать ещё", f"ll:{status}:{rows[PAGE - 1][0].id}")]]))
    await cb.answer()


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
        if action in ("nogrp", "yesgrp"):
            if not user.is_admin or not lead.group_id:
                return await cb.answer("Недоступно", show_alert=True)
            g = await s.get(Group, lead.group_id)
            if g is None:
                return await cb.answer("Группа не найдена", show_alert=True)
            g.no_leads = action == "nogrp"
            try:
                await lead_actions.apply_status(s, user, d, lead, "reject" if g.no_leads else "restore")
            except lead_actions.ActionError:
                pass  # статус уже такой — не мешает
            await s.commit()
            if g.no_leads:
                markup = kb([[("✔ Заявки из группы выключены", "noop"), ("↩️ Вернуть", f"lead:yesgrp:{d.id}")]])
                note = "Из этой группы заявки больше не берём (🔍 Группы → 🙈 Без заявок)"
            else:
                markup = notify.lead_keyboard(lead, d.id, user.is_admin)
                note = "Снова берём заявки из этой группы"
            await cb.message.edit_reply_markup(reply_markup=markup)
            return await cb.answer(note, show_alert=g.no_leads)
        if action == "take":
            client = await lead_actions.take(s, user, d, lead)
            await s.commit()
            await cb.message.edit_reply_markup(reply_markup=kb([[("👤 Открыть клиента", f"cl:open:{client.id}")]]))
            return await cb.answer("Клиент в CRM")
        try:
            note = await lead_actions.apply_status(s, user, d, lead, action)
        except lead_actions.ActionError as e:
            return await cb.answer(str(e), show_alert=action == "restore")
        await s.commit()
    if action == "restore":
        markup = notify.lead_keyboard(lead, d.id, user.is_admin)  # снова все кнопки, как у новой
    else:  # не тупик: ошибся кнопкой — можно вернуть
        rows = [[(f"✔ {note}", "noop"), ("↩️ Вернуть", f"lead:restore:{d.id}")]]
        if action in ("later", "reject"):  # «Не моё» по рекламе услуг — можно ещё научить фильтр
            rows.append([("🙅 Исполнитель (научить фильтр)", f"lead:offer:{d.id}")])
        markup = kb(rows)
    await cb.message.edit_reply_markup(reply_markup=markup)
    await cb.answer(note)


@router.callback_query(F.data == "noop")
async def noop(cb: CallbackQuery) -> None:
    await cb.answer()


def reply_rows(lead: Lead, text: str) -> list[list[tuple[str, str]]]:
    """Кнопки под готовым ответом. Бот сам клиенту не пишет (Telegram не даёт ботам писать первыми,
    разведчик только читает) — поэтому: одно нажатие копирует текст, второе открывает сообщение клиента в группе."""
    rows = []
    first = []
    if len(text) <= COPY_MAX:
        first.append(("📋 Скопировать ответ", f"copy:{text}"))
    if lead.link:
        first.append(("💬 Ответить в группе", f"url:{lead.link}"))
    if first:
        rows.append(first)
    contact = f"https://t.me/{lead.username}" if lead.username else f"tg://user?id={lead.author_id}"
    rows.append([("✍️ Написать в личку", f"url:{contact}")])
    return rows


def reply_hint(lead: Lead, text: str) -> str:
    """Короткая подсказка, как отправить ответ, — под текст ответа."""
    copy = "«📋 Скопировать ответ»" if len(text) <= COPY_MAX else "нажмите на текст ответа (скопируется)"
    if lead.link:
        where = ("«💬 Ответить в группе» → свайп по сообщению клиента влево → вставьте текст"
                 + ("" if lead.username else ". В личку этому человеку, скорее всего, не написать — он скрыл профиль"))
        if "/c/" in lead.link:
            where += ". Группа закрытая: сообщение откроется, только если вы в ней состоите"
        return f"\n\n<i>Как ответить: {copy} → {where}.</i>"
    return f"\n\n<i>Как ответить: {copy} → «✍️ Написать в личку».</i>"


async def send_template(cb: CallbackQuery, s, lead: Lead, user: User) -> None:
    """Ответ клиенту: шаблон и цены пользователя; у администратора — ещё и из категории."""
    text = await lead_actions.template_text(s, user, lead)
    await cb.message.answer(
        f"📋 Ответ клиенту:\n\n<code>{esc(text)}</code>{reply_hint(lead, text)}\n\n"
        "<i>Свой шаблон и цены: 👤 Профиль → 📋 Мой шаблон / 💰 Мои цены.</i>",
        reply_markup=kb(reply_rows(lead, text)),
        disable_web_page_preview=True,
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
        f"✨ <b>Черновик ответа</b> (поправьте под себя):\n\n<code>{esc(text)}</code>{reply_hint(lead, text)}",
        reply_markup=kb(reply_rows(lead, text) + [[("🔄 Другой вариант", f"lead:ai:{delivery_id}")]]),
        disable_web_page_preview=True)
