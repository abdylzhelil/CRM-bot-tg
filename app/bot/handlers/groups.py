from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select

from app.bot import notify
from app.bot.common import ask, esc, kb, reply
from app.db import Session
from app.models import Group, SearchQuery

router = Router()
# Менеджер разведчиков подставляется из main.py
manager = None
STATUS = {"candidate": "кандидаты", "approved": "очередь", "joined": "вступили", "skipped": "пропущены", "left": "вышли"}


@router.message(F.text == "🔍 Группы")
async def groups_menu(target: Message | CallbackQuery) -> None:
    async with Session() as s:
        cnt = dict((await s.execute(select(Group.status, func.count()).group_by(Group.status))).all())
        bot_groups = await s.scalar(select(func.count()).select_from(Group).where(
            Group.status == "joined", Group.account_id.is_(None)))
    me = await target.bot.me()
    privacy = "" if me.can_read_all_group_messages else (
        "\n⚠️ Privacy mode включён — бот не видит сообщения в группах, где он не админ. "
        "@BotFather → /setprivacy → Disable."
    )
    text = (
        f"🤖 Бот в группах: {bot_groups}{privacy}\n"
        "🔍 Разведчик: " + ", ".join(f"{v} {cnt.get(k, 0)}" for k, v in STATUS.items())
    )
    await reply(target, text, kb([
        [("➕ Добавить бота в группу", f"url:https://t.me/{me.username}?startgroup=true")],
        [("🤖 Группы бота", "gr:bot"), ("❓ Как это работает", "gr:help")],
        [("▶️ Искать сейчас", "gr:search"), ("🔄 Ротация сейчас", "gr:rotate")],
        [(f"⏳ Очередь {cnt.get('approved', 0)}", "gr:list:approved"), (f"❓ Кандидаты {cnt.get('candidate', 0)}", "gr:list:candidate")],
        [("🏆 Рейтинг вступивших", "gr:list:joined"), ("🔎 Поисковые запросы", "gq:list")],
    ]))


@router.callback_query(F.data == "gr:bot")
async def bot_groups(cb: CallbackQuery) -> None:
    async with Session() as s:
        groups = (await s.scalars(select(Group).where(
            Group.status == "joined", Group.account_id.is_(None)).order_by(Group.leads_count.desc()).limit(30))).all()
    if not groups:
        return await cb.answer("Бот пока не добавлен ни в одну группу", show_alert=True)
    lines = [f"• {esc(g.title)} — {g.members} уч., заявок: {g.leads_count}" for g in groups]
    await reply(cb, "🤖 Группы, где сидит бот:\n" + "\n".join(lines), kb([[("◀️ Назад", "gr:menu")]]))


@router.callback_query(F.data == "gr:help")
async def bot_help(cb: CallbackQuery) -> None:
    await reply(cb, (
        "🤖 <b>Бот в группах</b> — заявки без аккаунта-разведчика.\n\n"
        "1. @BotFather → /setprivacy → выберите бота → <b>Disable</b> (один раз).\n"
        "2. Добавьте бота в группу кнопкой «➕ Добавить бота в группу» — нужны права админа группы, "
        "или попросите админа добавить его.\n"
        "3. Бот молча читает сообщения и присылает вам заявки. В группе он ничего не пишет.\n\n"
        "Где взять группы: ваши группы, группы знакомых, а также админы, которым нужен бот-антиспам."
    ), kb([[("◀️ Назад", "gr:menu")]]))


@router.callback_query(F.data.startswith("gr:list:"))
async def group_list(cb: CallbackQuery) -> None:
    status = cb.data.split(":")[2]
    async with Session() as s:
        q = select(Group).where(Group.status == status)
        q = q.order_by(Group.leads_count.desc() if status == "joined" else Group.members.desc()).limit(15)
        groups = (await s.scalars(q)).all()
    if not groups:
        return await cb.answer("Пусто", show_alert=True)
    if status == "candidate":
        await cb.answer()
        for g in groups[:10]:
            await notify.send_group_card(g)
        return
    lines = [f"• {esc(g.title)} — {g.members} уч., заявок: {g.leads_count}" for g in groups]
    await reply(cb, f"{STATUS[status].capitalize()}:\n" + "\n".join(lines), kb([[("◀️ Назад", "gr:menu")]]))


@router.callback_query(F.data == "gr:menu")
async def back(cb: CallbackQuery) -> None:
    await groups_menu(cb)


@router.callback_query(F.data.regexp(r"^grp:(join|skip):\d+$"))
async def group_decision(cb: CallbackQuery) -> None:
    _, action, gid = cb.data.split(":")
    async with Session() as s:
        g = await s.get(Group, int(gid))
        g.status = "approved" if action == "join" else "skipped"
        await s.commit()
    await cb.message.edit_reply_markup(reply_markup=kb([[("✔ В очереди на вступление" if action == "join" else "✔ Пропущена", "noop")]]))
    await cb.answer()


@router.callback_query(F.data == "gr:search")
async def search_now(cb: CallbackQuery) -> None:
    import asyncio

    from app.scout.search import discover

    if not manager or not manager.clients:
        return await cb.answer("Нет подключённых разведчиков", show_alert=True)
    await cb.answer("Поиск запущен, карточки придут сами")
    for client in manager.clients.values():
        asyncio.create_task(discover(client))


@router.callback_query(F.data == "gr:rotate")
async def rotate_now(cb: CallbackQuery) -> None:
    import asyncio

    from app.scout.joiner import rotate

    if not manager or not manager.clients:
        return await cb.answer("Нет подключённых разведчиков", show_alert=True)
    await cb.answer("Ротация запущена")
    for acc_id, client in manager.clients.items():
        asyncio.create_task(rotate(client, acc_id))


@router.callback_query(F.data == "gq:list")
async def queries(cb: CallbackQuery) -> None:
    async with Session() as s:
        qs = (await s.scalars(select(SearchQuery).order_by(SearchQuery.id))).all()
    rows = [[(("✅ " if q.enabled else "⏸ ") + q.text, f"gq:tog:{q.id}"), ("🗑", f"gq:del:{q.id}")] for q in qs]
    rows.append([("➕ Добавить", "gq:add"), ("◀️ Назад", "gr:menu")])
    await reply(cb, "🔎 Поисковые запросы (нажмите, чтобы вкл/выкл):", kb(rows))


@router.callback_query(F.data.regexp(r"^gq:(tog|del):\d+$"))
async def query_edit(cb: CallbackQuery) -> None:
    _, action, qid = cb.data.split(":")
    async with Session() as s:
        q = await s.get(SearchQuery, int(qid))
        if q:
            if action == "tog":
                q.enabled = not q.enabled
            else:
                await s.delete(q)
            await s.commit()
    await queries(cb)


@router.callback_query(F.data == "gq:add")
async def query_add(cb: CallbackQuery, state: FSMContext) -> None:
    await ask(cb, state, "query_add", "Новые запросы (через запятую или с новой строки):")
