from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import delete, select

from app.bot.common import ask, esc, kb, reply
from app.db import Session
from app.models import Category, Keyword
from app.services import rules

router = Router()


@router.message(F.text == "🗂 Категории")
async def categories_menu(target: Message | CallbackQuery) -> None:
    async with Session() as s:
        roots = (await s.scalars(select(Category).where(Category.parent_id.is_(None)).order_by(Category.id))).all()
    rows = [[(("" if c.enabled else "⏸ ") + f"{c.emoji} {c.name}", f"cat:open:{c.id}")] for c in roots]
    rows.append([("➕ Новая категория", "cat:add:0")])
    await reply(target, "🗂 Категории:", kb(rows))


@router.callback_query(F.data == "cat:menu")
async def back(cb: CallbackQuery) -> None:
    await categories_menu(cb)


@router.callback_query(F.data.startswith("cat:open:"))
async def open_cat(cb: CallbackQuery) -> None:
    cid = int(cb.data.split(":")[2])
    async with Session() as s:
        c = await s.get(Category, cid)
        subs = (await s.scalars(select(Category).where(Category.parent_id == cid).order_by(Category.id))).all()
    text = (
        f"{c.emoji} <b>{esc(c.name)}</b> {'' if c.enabled else '(выключена)'}\n"
        f"Город: {esc(c.city) or 'любой'}\n"
        f"Шаблон ответа: {esc(c.reply_template[:200]) or '—'}\nЦены: {esc(c.prices[:200]) or '—'}"
    )
    rows = [[(("" if x.enabled else "⏸ ") + x.name, f"cat:open:{x.id}")] for x in subs]
    rows += [
        [("🔑 Слова", f"kw:view:category:{cid}"), ("💎 Сильные фразы", f"kw:view:strong:{cid}"), ("⛔ Минус", f"kw:view:negative:{cid}")],
        [("📋 Шаблон", f"cat:tpl:{cid}"), ("💰 Цены", f"cat:prices:{cid}"), ("📍 Город", f"cat:city:{cid}")],
        [("✏️ Переименовать", f"cat:ren:{cid}"), ("⏯ Вкл/выкл", f"cat:tog:{cid}"), ("🗑 Удалить", f"cat:del:{cid}")],
        [("➕ Подкатегория", f"cat:add:{cid}"), ("◀️ Назад", f"cat:open:{c.parent_id}" if c.parent_id else "cat:menu")],
    ]
    await reply(cb, text, kb(rows))


@router.callback_query(F.data.startswith("cat:tog:"))
async def toggle(cb: CallbackQuery) -> None:
    async with Session() as s:
        c = await s.get(Category, int(cb.data.split(":")[2]))
        c.enabled = not c.enabled
        await s.commit()
    rules.invalidate()
    await open_cat(cb)


@router.callback_query(F.data.startswith("cat:del:"))
async def delete_cat(cb: CallbackQuery) -> None:
    cid = int(cb.data.split(":")[2])
    async with Session() as s:
        c = await s.get(Category, cid)
        ids = [cid] + list((await s.scalars(select(Category.id).where(Category.parent_id == cid))).all())
        await s.execute(delete(Keyword).where(Keyword.category_id.in_(ids)))
        await s.execute(delete(Category).where(Category.parent_id == cid))
        await s.delete(c)
        await s.commit()
        parent = c.parent_id
    rules.invalidate()
    if parent:
        cb.data = f"cat:open:{parent}"
        return await open_cat(cb)
    await categories_menu(cb)


@router.callback_query(F.data.regexp(r"^cat:(add|ren|tpl|prices|city):\d+$"))
async def edit(cb: CallbackQuery, state: FSMContext) -> None:
    _, field, cid = cb.data.split(":")
    prompts = {
        "add": "Название новой категории/подкатегории:",
        "ren": "Новое название:",
        "tpl": "Шаблон ответа клиенту:",
        "prices": "Цены (текстом):",
        "city": "Город-фильтр (например, Махачкала) или «-», чтобы убрать:",
    }
    await ask(cb, state, f"cat_{field}", prompts[field], id=int(cid))
