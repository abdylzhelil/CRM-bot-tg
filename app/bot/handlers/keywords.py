from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select

from app.bot.common import ask, esc, kb, reply
from app.db import Session
from app.models import Category, Keyword

router = Router()
KINDS = {
    "intent": "Слова-намерения",
    "negative": "Минус-слова",
    "category": "Слова категории",
    "strong": "Сильные фразы",
}


@router.message(F.text == "🔑 Ключевые слова")
async def keywords_menu(target: Message | CallbackQuery) -> None:
    await reply(target, "🔑 Общие списки (слова категорий — в разделе «Категории»):", kb([
        [("🎯 Слова-намерения", "kw:view:intent:0")],
        [("⛔ Глобальные минус-слова", "kw:view:negative:0")],
    ]))


@router.callback_query(F.data.startswith("kw:view:"))
async def view(cb: CallbackQuery) -> None:
    _, _, kind, cid = cb.data.split(":")
    cid = int(cid)
    async with Session() as s:
        words = (await s.scalars(select(Keyword.word).where(
            Keyword.kind == kind, Keyword.category_id == (cid or None)).order_by(Keyword.id))).all()
        cat = await s.get(Category, cid) if cid else None
    title = KINDS[kind] + (f" · {cat.name}" if cat else "")
    back = f"cat:open:{cid}" if cid else "kw:menu"
    await reply(cb, f"<b>{esc(title)}</b>\n" + (esc(", ".join(words)) or "— пусто —"), kb([
        [("➕ Добавить", f"kw:add:{kind}:{cid}"), ("➖ Удалить", f"kw:del:{kind}:{cid}")],
        [("◀️ Назад", back)],
    ]))


@router.callback_query(F.data == "kw:menu")
async def back(cb: CallbackQuery) -> None:
    await keywords_menu(cb)


@router.callback_query(F.data.regexp(r"^kw:(add|del):\w+:\d+$"))
async def edit(cb: CallbackQuery, state: FSMContext) -> None:
    _, op, kind, cid = cb.data.split(":")
    verb = "Добавить" if op == "add" else "Удалить"
    await ask(cb, state, f"kw_{op}", f"{verb} слова/фразы (через запятую или с новой строки). "
              "Склонения учитываются автоматически.", kind=kind, cid=int(cid))
