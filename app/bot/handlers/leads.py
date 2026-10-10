from aiogram import F, Router
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select

from app.bot import notify
from app.bot.common import esc, kb, reply
from app.db import Session
from app.models import BlacklistEntry, Category, Client, ClientEvent, Group, Lead

router = Router()


async def category_name(s, cat_id: int | None) -> str:
    cat = await s.get(Category, cat_id) if cat_id else None
    if not cat:
        return "—"
    parent = await s.get(Category, cat.parent_id) if cat.parent_id else None
    return f"{(parent or cat).emoji} {parent.name + ' → ' if parent else ''}{cat.name}".strip()


@router.message(F.text == "📥 Заявки")
async def leads_menu(m: Message) -> None:
    async with Session() as s:
        cnt = dict((await s.execute(select(Lead.status, func.count()).group_by(Lead.status))).all())
        rows = (await s.scalars(
            select(Lead).where(Lead.status.in_(["new", "postponed"])).order_by(Lead.id.desc()).limit(5)
        )).all()
        await m.answer(
            f"📥 Заявки: новых {cnt.get('new', 0)}, отложено {cnt.get('postponed', 0)}, "
            f"в работе {cnt.get('taken', 0)}, отказ {cnt.get('rejected', 0)}"
            + ("\nПоследние:" if rows else "\nПока нет заявок — разведчик читает группы.")
        )
        for lead in rows:
            await m.answer(notify.lead_text(lead, await category_name(s, lead.category_id)),
                           reply_markup=notify.lead_keyboard(lead), disable_web_page_preview=True)


async def _set_blacklist(s, lead: Lead, status: str, reason: str) -> None:
    entry = await s.get(BlacklistEntry, lead.author_id)
    if entry:
        entry.status, entry.reason = status, reason
    else:
        s.add(BlacklistEntry(user_id=lead.author_id, username=lead.username, status=status, reason=reason))


@router.callback_query(F.data.startswith("lead:"))
async def lead_action(cb: CallbackQuery) -> None:
    _, action, raw_id = cb.data.split(":")
    async with Session() as s:
        lead = await s.get(Lead, int(raw_id))
        if not lead:
            return await cb.answer("Заявка не найдена")
        note = ""
        if action == "take":
            client = await s.scalar(select(Client).where(Client.tg_id == lead.author_id))
            if not client:
                group = await s.get(Group, lead.group_id) if lead.group_id else None
                client = Client(
                    tg_id=lead.author_id, name=lead.name, username=lead.username,
                    source=group.title if group else lead.group_title, category_id=lead.category_id,
                )
                s.add(client)
                await s.flush()
            s.add(ClientEvent(client_id=client.id, text=f"Заявка: {lead.text[:300]}"))
            lead.status = "taken"
            await s.commit()
            await cb.message.edit_reply_markup(reply_markup=kb([[("👤 Открыть клиента", f"cl:open:{client.id}")]]))
            return await cb.answer("Клиент в CRM")
        if action == "later":
            lead.status, note = "postponed", "Отложено"
        elif action == "reject":
            lead.status, note = "rejected", "Отказ"
        elif action == "spam":
            lead.status, note = "spam", "В чёрном списке"
            await _set_blacklist(s, lead, "spam", "отмечено владельцем")
        elif action == "client":
            await _set_blacklist(s, lead, "client", "отмечено владельцем")
            lead.suspicious, note = False, "В белом списке"
        await s.commit()
    await cb.message.edit_reply_markup(reply_markup=kb([[(f"✔ {note}", "noop")]]))
    await cb.answer(note)


@router.callback_query(F.data == "noop")
async def noop(cb: CallbackQuery) -> None:
    await cb.answer()
