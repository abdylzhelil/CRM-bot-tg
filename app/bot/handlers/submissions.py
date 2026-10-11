"""Администратор: проверка групп, добавленных пользователями."""
from aiogram import F, Router
from aiogram.types import CallbackQuery
from sqlalchemy import select

from app.bot.common import esc, kb, reply
from app.bot.handlers.catalog import KIND
from app.db import Session
from app.models import Category, Group, User
from app.services import catalog

router = Router()


async def card(g: Group) -> tuple[str, object]:
    async with Session() as s:
        cat = await s.get(Category, g.category_id) if g.category_id else None
        who = await s.get(User, g.submitted_by)
    name = (who.name or str(who.tg_id)) if who else str(g.submitted_by)
    text = (
        f"{KIND.get(g.kind, '')} <b>{esc(g.title)}</b> — <a href=\"https://t.me/{esc(g.username)}\">@{esc(g.username)}</a>\n"
        f"Участников: {g.members} · {'канал' if g.kind == 'channel' else 'группа'}\n"
        f"Ниша: {esc(cat.name) if cat else '—'} · город: {esc(g.city or 'вся Россия')}\n"
        f"Добавил(а): {esc(name)}"
    )
    return text, kb([[("✅ Одобрить", f"up:ok:{g.id}"), ("❌ Отклонить", f"up:no:{g.id}")]])


@router.callback_query(F.data == "up:list")
async def pending_list(cb: CallbackQuery) -> None:
    async with Session() as s:
        groups = (await s.scalars(select(Group).where(Group.status == "pending").order_by(Group.submitted_at).limit(5))).all()
        total = await catalog.pending_count(s)
    if not groups:
        return await reply(cb, "📝 Новых групп от пользователей нет.", kb([[("◀️ Назад", "gr:menu")]]))
    await cb.answer()
    await cb.message.answer(f"📝 Группы на проверке: {total} (показаны первые {len(groups)})")
    for g in groups:
        text, markup = await card(g)
        await cb.message.answer(text, reply_markup=markup, disable_web_page_preview=True)


@router.callback_query(F.data.regexp(r"^up:(ok|no):\d+$"))
async def decide(cb: CallbackQuery) -> None:
    _, action, gid = cb.data.split(":")
    async with Session() as s:
        g = await s.get(Group, int(gid))
        if g is None or g.status != "pending":
            return await cb.answer("Уже обработано", show_alert=True)
        title, owner = g.title, g.submitted_by
        if action == "ok":
            await catalog.approve(s, g)
        else:
            await catalog.reject(s, g)
    await cb.message.edit_reply_markup(reply_markup=kb([[("✔ Одобрена" if action == "ok" else "✔ Отклонена", "noop")]]))
    await cb.answer()
    text = (f"✅ Ваша группа «{esc(title)}» одобрена и добавлена в каталог. За заявки из неё вы получите бонусы 🎁"
            if action == "ok" else f"❌ Группа «{esc(title)}» не прошла проверку.")
    try:
        await cb.bot.send_message(owner, text)
    except Exception:
        pass
