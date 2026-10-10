from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select

from app.bot.common import ask, esc, kb, reply
from app.db import Session
from app.models import BlacklistEntry

router = Router()


@router.message(F.text == "🛡 Чёрный список")
async def blacklist_menu(target: Message | CallbackQuery) -> None:
    async with Session() as s:
        spam = await s.scalar(select(func.count()).select_from(BlacklistEntry).where(BlacklistEntry.status == "spam"))
        white = await s.scalar(select(func.count()).select_from(BlacklistEntry).where(BlacklistEntry.status == "client"))
        last = (await s.scalars(select(BlacklistEntry).where(BlacklistEntry.status == "spam")
                                .order_by(BlacklistEntry.created_at.desc()).limit(10))).all()
    rows = [[(f"♻️ {e.username and '@' + e.username or e.user_id}", f"bl:rm:{e.user_id}")] for e in last]
    rows.append([("➕ Добавить по ID", "bl:add")])
    await reply(target, f"🛡 Чёрный список: {spam}, белый: {white}\nНажмите на запись, чтобы убрать из списка:", kb(rows))


@router.callback_query(F.data.startswith("bl:rm:"))
async def remove(cb: CallbackQuery) -> None:
    async with Session() as s:
        e = await s.get(BlacklistEntry, int(cb.data.split(":")[2]))
        if e:
            await s.delete(e)
            await s.commit()
    await blacklist_menu(cb)


@router.callback_query(F.data == "bl:add")
async def add(cb: CallbackQuery, state: FSMContext) -> None:
    await ask(cb, state, "bl_add", "Telegram ID пользователя (числом):")
