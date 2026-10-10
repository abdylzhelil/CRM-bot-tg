from datetime import timedelta

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, or_, select

from app.bot.common import ask, esc, kb, reply
from app.bot.handlers.leads import category_name
from app.db import Session
from app.models import STAGE_NAMES, STAGES, Client, ClientEvent, Reminder, now

router = Router()
PAGE = 20


@router.message(F.text == "👥 Клиенты")
async def clients_menu(m: Message) -> None:
    await show_list(m, "all")


async def show_list(target, stage: str, query: str = "") -> None:
    async with Session() as s:
        counts = dict((await s.execute(select(Client.stage, func.count()).group_by(Client.stage))).all())
        q = select(Client).order_by(Client.updated_at.desc()).limit(PAGE)
        if stage != "all":
            q = q.where(Client.stage == stage)
        if query:
            like = f"%{query.lstrip('@')}%"
            q = q.where(or_(Client.name.ilike(like), Client.username.ilike(like), Client.phone.ilike(like)))
        clients = (await s.scalars(q)).all()
    filters = [(f"{label} {counts.get(k, 0)}", f"cl:list:{k}") for k, label in STAGES]
    rows = [filters[i : i + 3] for i in range(0, len(filters), 3)]
    rows.append([(f"Все {sum(counts.values())}", "cl:list:all"), ("🔎 Поиск", "cl:find")])
    for c in clients:
        who = c.name or c.username or str(c.tg_id)
        rows.append([(f"{STAGE_NAMES[c.stage][:2]} {who}"[:60], f"cl:open:{c.id}")])
    title = "👥 Клиенты" + (f" · {STAGE_NAMES[stage]}" if stage != "all" else "") + (f" · «{esc(query)}»" if query else "")
    await reply(target, title if clients else title + "\nПусто.", kb(rows))


@router.callback_query(F.data.startswith("cl:list:"))
async def cb_list(cb: CallbackQuery) -> None:
    await show_list(cb, cb.data.split(":")[2])


@router.callback_query(F.data == "cl:find")
async def cb_find(cb: CallbackQuery, state: FSMContext) -> None:
    await ask(cb, state, "client_find", "Имя, @username или телефон для поиска:")


async def card(target, client_id: int) -> None:
    async with Session() as s:
        c = await s.get(Client, client_id)
        if not c:
            return await reply(target, "Клиент не найден")
        events = (await s.scalars(
            select(ClientEvent).where(ClientEvent.client_id == c.id).order_by(ClientEvent.id.desc()).limit(5)
        )).all()
        rem = await s.scalar(
            select(Reminder).where(Reminder.client_id == c.id, ~Reminder.sent).order_by(Reminder.due_at)
        )
        cat = await category_name(s, c.category_id)
    lines = [
        f"👤 <b>{esc(c.name or '—')}</b> {('@' + esc(c.username)) if c.username else ''}",
        f"Этап: {STAGE_NAMES[c.stage]}",
        f"Телефон: {esc(c.phone) or '—'}",
        f"Категория: {esc(cat)}",
        f"Бюджет: {c.budget or '—'}",
        f"Источник: {esc(c.source) or '—'}",
    ]
    if rem:
        lines.append(f"⏰ Напоминание: {rem.due_at:%d.%m %H:%M} (UTC)")
    if c.notes:
        lines.append(f"\n📝 {esc(c.notes[-500:])}")
    if events:
        lines.append("\nИстория:\n" + "\n".join(f"• {e.created_at:%d.%m} {esc(e.text[:120])}" for e in events))
    stage_btns = [(label, f"cl:stage:{c.id}:{k}") for k, label in STAGES if k != c.stage]
    contact = f"url:https://t.me/{c.username}" if c.username else f"url:tg://user?id={c.tg_id}"
    rows = [stage_btns[i : i + 2] for i in range(0, len(stage_btns), 2)]
    rows += [
        [("📝 Заметка", f"cl:note:{c.id}"), ("💰 Бюджет", f"cl:budget:{c.id}"), ("☎️ Телефон", f"cl:phone:{c.id}")],
        [("⏰ 1 дн", f"cl:rem:{c.id}:1"), ("⏰ 3 дн", f"cl:rem:{c.id}:3"), ("⏰ 7 дн", f"cl:rem:{c.id}:7")],
        [("✍️ Написать", contact), ("◀️ К списку", "cl:list:all")],
    ]
    await reply(target, "\n".join(lines), kb(rows))


@router.callback_query(F.data.startswith("cl:open:"))
async def cb_open(cb: CallbackQuery) -> None:
    await card(cb, int(cb.data.split(":")[2]))


@router.callback_query(F.data.startswith("cl:stage:"))
async def cb_stage(cb: CallbackQuery) -> None:
    _, _, cid, stage = cb.data.split(":")
    async with Session() as s:
        c = await s.get(Client, int(cid))
        c.stage = stage
        s.add(ClientEvent(client_id=c.id, text=f"Этап → {STAGE_NAMES[stage]}"))
        if stage == "thinking":  # «дожать» через 2 дня, если клиент думает
            s.add(Reminder(client_id=c.id, due_at=now() + timedelta(days=2), text="Клиент думает — дожать"))
        await s.commit()
    await card(cb, int(cid))


@router.callback_query(F.data.startswith("cl:rem:"))
async def cb_rem(cb: CallbackQuery) -> None:
    _, _, cid, days = cb.data.split(":")
    async with Session() as s:
        s.add(Reminder(client_id=int(cid), due_at=now() + timedelta(days=int(days))))
        s.add(ClientEvent(client_id=int(cid), text=f"Напоминание через {days} дн."))
        await s.commit()
    await card(cb, int(cid))


@router.callback_query(F.data.regexp(r"^cl:(note|budget|phone):\d+$"))
async def cb_edit(cb: CallbackQuery, state: FSMContext) -> None:
    _, field, cid = cb.data.split(":")
    prompts = {"note": "Текст заметки:", "budget": "Бюджет клиента (число):", "phone": "Телефон клиента:"}
    await ask(cb, state, f"client_{field}", prompts[field], id=int(cid))
