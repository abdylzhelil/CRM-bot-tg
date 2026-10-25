from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select

from app.bot import notify
from app.bot.common import COPY_MAX, ask, esc, kb, reply
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


# ---------- 📥 Заявки: список → ниша → по одной заявке с «◀️ / ▶️» (просьба владельца 25.10) ----------
LABELS = dict(LISTS) | {"muted": "📥 Накопилось"}


def _filter(q, user_id: int, status: str):
    q = q.where(Delivery.user_id == user_id, Delivery.sent)
    # «🔕 накопилось»: пришли без уведомления и ещё не разобраны (они же есть в «🆕 Новые»)
    return q.where(Delivery.muted, Delivery.status == "new") if status == "muted" else q.where(Delivery.status == status)


async def _niche_ids(s, cat: int) -> list[int]:
    return [cat] + list((await s.scalars(select(Category.id).where(Category.parent_id == cat))).all())


async def _niches(s, user_id: int, status: str) -> list[tuple[int, str, int]]:
    """[(ниша верхнего уровня, название, сколько заявок)] в этом списке — для выбора ниши."""
    q = select(Lead.category_id, func.count()).select_from(Delivery).join(Lead, Lead.id == Delivery.lead_id)
    rows = (await s.execute(_filter(q, user_id, status).group_by(Lead.category_id))).all()
    agg: dict[int, int] = {}
    for cid, n in rows:
        root = await root_category(s, cid) or 0
        agg[root] = agg.get(root, 0) + n
    out = []
    for root, n in agg.items():
        cat = await s.get(Category, root) if root else None
        out.append((root, f"{cat.emoji} {cat.name}".strip() if cat else "Без ниши", n))
    return sorted(out, key=lambda x: -x[2])


async def menu_view(user: User) -> tuple[str, object]:
    async with Session() as s:
        cnt = await status_counts(s, user.tg_id)
        waiting = await s.scalar(select(func.count()).select_from(Delivery).where(Delivery.user_id == user.tg_id, ~Delivery.sent))
        row = await s.get(User, user.tg_id)
        muted_n = await mute.accumulated(s, user.tg_id)
    markup = lists_keyboard(cnt)
    if muted_n:
        markup.inline_keyboard.append(kb([[(f"📥 Накопилось без уведомлений · {muted_n}", "ll:muted:0")]]).inline_keyboard[0])
    markup.inline_keyboard.append(kb([[(f"🔕 Уведомления выключены {mute.until_text(row)} — включить" if mute.is_muted(row)
                                        else "🔕 Выключить уведомления на время", "mt:menu")]]).inline_keyboard[0])
    text = ("📥 <b>Заявки</b> — выберите список, потом нишу; заявки откроются по одной, листайте «◀️ / ▶️»."
            + (f"\n⏳ Ещё придут: {waiting}" if waiting else "")
            + (f"\n🔕 Уведомления выключены {mute.until_text(row)} — заявки копятся без сообщений." if mute.is_muted(row) else "")
            + ("" if cnt.get("new") or cnt.get("postponed") else "\nНовых заявок пока нет — пришлю, как только появятся."))
    return text, markup


@router.message(F.text == "📥 Заявки")
async def leads_menu(m: Message, user: User) -> None:
    text, markup = await menu_view(user)
    await m.answer(text, reply_markup=markup)


@router.callback_query(F.data == "ll:menu")
async def leads_menu_cb(cb: CallbackQuery, user: User) -> None:
    text, markup = await menu_view(user)
    await reply(cb, text, markup)


@router.callback_query(F.data.regexp(r"^ll:(new|postponed|taken|rejected|spam|muted):\d{1,18}$"))
async def leads_list(cb: CallbackQuery, user: User) -> None:
    """Список выбран: если ниш несколько — выбор ниши, иначе сразу первая заявка."""
    status = cb.data.split(":")[1]
    async with Session() as s:
        niches = await _niches(s, user.tg_id, status)
    total = sum(n for _, _, n in niches)
    label = LABELS[status]
    if not total:
        return await reply(cb, f"{label}: пусто.", kb([[("◀️ Списки", "ll:menu")]]))
    if len(niches) == 1:
        return await show_one(cb, user, status, 0, 0, "c")
    rows = [[(f"📂 Все ниши · {total}", f"lv:{status}:0:0:c")]]
    rows += [[(f"{name} · {n}"[:60], f"lv:{status}:{cid}:0:c")] for cid, name, n in niches]
    await reply(cb, f"{label}: {total}. Какую нишу смотреть?", kb(rows + [[("◀️ Списки", "ll:menu")]]))


@router.callback_query(F.data.regexp(r"^lv:(new|postponed|taken|rejected|spam|muted):\d{1,18}:\d{1,18}:[cnp]$"))
async def leads_view(cb: CallbackQuery, user: User) -> None:
    _, status, cat, did, way = cb.data.split(":")
    await show_one(cb, user, status, int(cat), int(did), way)


async def show_one(cb: CallbackQuery, user: User, status: str, cat: int, did: int, way: str) -> None:
    """Одна заявка. way: c — эта (или следующая, если её уже убрали из списка), n — следующая (старше), p — предыдущая.
    Листаем по номеру доставки, а не по позиции: после «Не моё»/«В работу» «▶️» ведёт к следующей, а не перескакивает."""
    async with Session() as s:
        q = _filter(select(Delivery, Lead).join(Lead, Lead.id == Delivery.lead_id), user.tg_id, status)
        if cat:
            q = q.where(Lead.category_id.in_(await _niche_ids(s, cat)))
        if way == "p":
            row = (await s.execute(q.where(Delivery.id > did).order_by(Delivery.id).limit(1))).first()
        else:
            if did:
                q2 = q.where(Delivery.id < did) if way == "n" else q.where(Delivery.id <= did)
            else:
                q2 = q
            row = (await s.execute(q2.order_by(Delivery.id.desc()).limit(1))).first()
            if row is None and did:  # дошли до конца — показываем последнюю (самую старую)
                row = (await s.execute(q.order_by(Delivery.id).limit(1))).first()
        count_q = select(func.count()).select_from(Delivery).join(Lead, Lead.id == Delivery.lead_id)
        count_q = _filter(count_q, user.tg_id, status)
        if cat:
            count_q = count_q.where(Lead.category_id.in_(await _niche_ids(s, cat)))
        total = await s.scalar(count_q) or 0
        label = LABELS[status] + (f" · {(await s.get(Category, cat)).name}" if cat and await s.get(Category, cat) else "")
        if row is None:
            return await reply(cb, f"{label}: пусто.", kb([[("◀️ Списки", "ll:menu")]]))
        d, lead = row
        pos = (await s.scalar(count_q.where(Delivery.id > d.id)) or 0) + 1
        client_id = None
        if d.status == "taken":
            client_id = await s.scalar(select(Client.id).where(Client.user_id == user.tg_id, Client.tg_id == lead.author_id))
        text = f"<b>{esc(label)}</b> — {pos} из {total}\n\n" + notify.lead_text(lead, await category_name(s, lead.category_id))
    card = card_keyboard(lead, d, user, client_id)
    await reply(cb, text, with_nav(card, nav_rows(status, cat, d.id, pos, total)))


def nav_rows(status: str, cat: int, did: int, pos: int, total: int) -> list:
    base = f"lv:{status}:{cat}:{did}"
    row = ([("◀️ Предыдущая", f"{base}:p")] if pos > 1 else []) + ([("Следующая ▶️", f"{base}:n")] if pos < total else [])
    return kb(([row] if row else []) + [[("📋 Списки", "ll:menu"), ("📂 Ниши", f"ll:{status}:0")]]).inline_keyboard


def with_nav(card, nav: list):
    from aiogram.types import InlineKeyboardMarkup

    return InlineKeyboardMarkup(inline_keyboard=list(card.inline_keyboard if card else []) + nav)


def _nav_of(cb: CallbackQuery) -> list:
    """Строки «◀️ / ▶️ / Списки» из сообщения, где нажали кнопку (заявка открыта в просмотре по одной)."""
    markup = getattr(cb.message, "reply_markup", None) if cb.message else None
    rows = getattr(markup, "inline_keyboard", None) or []
    return [r for r in rows if r and all((b.callback_data or "").startswith(("lv:", "ll:")) for b in r)]


async def edit_markup(cb: CallbackQuery, markup) -> None:
    """Сменить кнопки под заявкой, сохранив навигацию просмотра (если она есть)."""
    nav = _nav_of(cb)
    await cb.message.edit_reply_markup(reply_markup=with_nav(markup, nav) if nav else markup)


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
            await edit_markup(cb, markup)
            return await cb.answer(note, show_alert=g.no_leads)
        if action == "take":
            client = await lead_actions.take(s, user, d, lead)
            await s.commit()
            await edit_markup(cb, kb([[("👤 Открыть клиента", f"cl:open:{client.id}")]]))
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
    await edit_markup(cb, markup)
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
    if lead.link and not lead.gone_at:
        first.append(("💬 Ответить в группе", f"url:{lead.link}"))
    if first:
        rows.append(first)
    contact = f"https://t.me/{lead.username}" if lead.username else f"tg://user?id={lead.author_id}"
    rows.append([("✍️ Написать в личку", f"url:{contact}")])
    return rows


def reply_hint(lead: Lead, text: str) -> str:
    """Короткая подсказка, как отправить ответ, — под текст ответа."""
    copy = "«📋 Скопировать ответ»" if len(text) <= COPY_MAX else "нажмите на текст ответа (скопируется)"
    if lead.gone_at:
        return f"\n\n<i>Сообщение уже удалено в группе. Как ответить: {copy} → «✍️ Написать в личку».</i>"
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
