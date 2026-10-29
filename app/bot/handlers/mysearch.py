"""«🔎 Найти группы для меня» (🌐 Каталог): свои слова для поиска групп разведчиком. Логика — services/mysearch.py."""
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select

from app.bot.common import ask, esc, kb, reply
from app.db import Session
from app.models import User, UserSearch
from app.services import mysearch

router = Router()        # всем пользователям
admin_router = Router()  # админу: все запросы пользователей

INTRO = ("🔎 <b>Найти группы для меня</b>\n\n"
         "Напишите, какие группы искать, например «ремонт Подольск» или «свадьбы Казань». Наш разведчик найдёт открытые "
         "группы по этим словам, проверит их (рекламные свалки и флуды пропускает) и вступит. Заявки оттуда по вашим нишам "
         "вы будете получать в первую очередь. Ваш аккаунт и номер не нужны.\n")


async def show(target: Message | CallbackQuery, user: User) -> None:
    async with Session() as s:
        mine = await mysearch.my(s, user.tg_id)
        limit = await mysearch.limit_for(s, user)
        lines = [f"{i}. «{esc(q.text)}» — {mysearch.describe(q, await mysearch.counts(s, q.id))}"
                 for i, q in enumerate(mine, 1)]
    text = INTRO + (f"\n<b>Ваши запросы ({len(mine)} из {limit}):</b>\n" + "\n".join(lines) if mine
                    else f"\nУ вас пока нет запросов. Можно добавить до {limit}.")
    if not limit and not user.is_admin:
        text += "\n\nНа бесплатном тарифе функция выключена — она доступна с 💎."
    rows = [[(f"🗑 «{q.text[:30]}»", f"ms:del:{q.id}")] for q in mine]
    if len(mine) < limit:
        rows.insert(0, [("➕ Добавить запрос", "ms:add")])
    rows.append([("◀️ Каталог", "cg:menu")])
    await reply(target, text, kb(rows))


@router.callback_query(F.data == "ms:list")
async def ms_list(cb: CallbackQuery, user: User) -> None:
    await show(cb, user)


@router.callback_query(F.data == "ms:add")
async def ms_add(cb: CallbackQuery, state: FSMContext) -> None:
    await ask(cb, state, "my_search", "Какие группы искать? Напишите услугу и город, например: «ремонт Подольск».")


@router.callback_query(F.data.regexp(r"^ms:del:\d+$"))
async def ms_del(cb: CallbackQuery) -> None:
    sid = cb.data.split(":")[2]
    await reply(cb, "Удалить запрос? Уже найденные группы разведчик продолжит читать, но заявки оттуда больше не будут "
                    "приходить вам в первую очередь.", kb([[("🗑 Да, удалить", f"ms:delok:{sid}"), ("◀️ Нет", "ms:list")]]))


@router.callback_query(F.data.regexp(r"^ms:delok:\d+$"))
async def ms_delok(cb: CallbackQuery, user: User) -> None:
    sid = int(cb.data.split(":")[2])
    async with Session() as s:
        q = await s.get(UserSearch, sid)
        foreign = q is not None and q.user_id != user.tg_id
        ok = await mysearch.remove(s, user.tg_id, sid, admin=user.is_admin)
        await s.commit()
    await cb.answer("Удалено" if ok else "Запрос уже удалён")
    if foreign and user.is_admin:
        return await ms_all(cb)
    await show(cb, user)


async def on_input(s, m: Message, d, text: str):
    """Ввод слов для поиска (inputs.HANDLERS["my_search"])."""
    user = await s.get(User, m.from_user.id)
    try:
        q = await mysearch.add(s, user, text)
    except mysearch.SearchError as e:
        return await m.answer(f"⚠️ {esc(str(e))}") and False
    await s.commit()
    await m.answer(f"✔ Запрос «{esc(q.text)}» добавлен. Разведчик начнёт поиск в ближайшие минуты и пришлёт итог.",
                   reply_markup=kb([[("🔎 Мои поиски групп", "ms:list")]]))


# ---- админ: все запросы пользователей ----
@admin_router.callback_query(F.data == "ms:all")
async def ms_all(cb: CallbackQuery) -> None:
    async with Session() as s:
        rows = (await s.execute(select(UserSearch, User.name).join(User, User.tg_id == UserSearch.user_id, isouter=True)
                                .order_by(UserSearch.id.desc()).limit(30))).all()
        total = await s.scalar(select(func.count()).select_from(UserSearch)) or 0
        lines = [f"• «{esc(q.text)}» — {esc(name or str(q.user_id))}: {mysearch.describe(q, await mysearch.counts(s, q.id))}"
                 for q, name in rows]
    buttons = [[(f"🗑 «{q.text[:25]}» ({(name or str(q.user_id))[:15]})", f"ms:del:{q.id}")] for q, name in rows]
    buttons.append([("◀️ Назад", "gq:list")])
    await reply(cb, (f"🙋 <b>Поиск групп для пользователей</b> (всего {total}, показаны последние 30)\n"
                     "Разведчик ищет по ним сначала, потом по общим запросам. Лимиты — ⚙️ → 🔍 Разведчик.\n\n"
                     + ("\n".join(lines) if lines else "Пока никто не добавил.")), kb(buttons))
