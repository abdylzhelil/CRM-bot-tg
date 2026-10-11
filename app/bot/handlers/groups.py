from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select

from app.bot.common import ask, esc, kb, reply
from app.db import Session
from app.models import FeedChat, Group, SearchQuery
from app.services import catalog, settings

router = Router()
# Менеджер разведчиков подставляется из main.py
manager = None
STATUS = {
    "candidate": "кандидаты", "approved": "очередь", "joined": "вступили", "covered": "уже с нашим ботом",
    "skipped": "пропущены", "left": "вышли",
}
REVIEW_PAGE = 8


@router.message(F.text == "🔍 Группы")
async def groups_menu(target: Message | CallbackQuery) -> None:
    async with Session() as s:
        cnt = dict((await s.execute(select(Group.status, func.count()).group_by(Group.status))).all())
        bot_groups = await s.scalar(select(func.count()).select_from(Group).where(
            Group.status == "joined", Group.account_id.is_(None), Group.kind == "group"))
        pending = await catalog.pending_count(s)
        paused = await settings.get(s, "scout_paused") == "1"
    me = await target.bot.me()
    privacy = "" if me.can_read_all_group_messages else (
        "\n⚠️ Privacy mode включён — бот не видит сообщения в группах, где он не админ. "
        "@BotFather → /setprivacy → Disable."
    )
    text = (
        f"🤖 Бот в группах: {bot_groups}{privacy}\n"
        f"🔍 Разведчик{' (⏸ на паузе)' if paused else ''}: "
        + ", ".join(f"{v} {cnt.get(k, 0)}" for k, v in STATUS.items())
    )
    await reply(target, text, kb([
        [("➕ Добавить бота в группу", f"url:https://t.me/{me.username}?startgroup=true")],
        [("🤖 Группы бота", "gr:bot"), ("❓ Как это работает", "gr:help")],
        [("🛡 Бот модерации", "fd:list"), (f"📝 От пользователей {pending}", "up:list")],
        [("▶️ Искать сейчас", "gr:search"), ("⏹ Остановить поиск", "gr:stop")],
        [("▶️ Возобновить разведчика" if paused else "⏸ Пауза разведчика", "gr:pause"),
         ("🔄 Ротация сейчас", "gr:rotate")],
        [(f"⏳ Очередь {cnt.get('approved', 0)}", "gr:list:approved"), (f"❓ Кандидаты {cnt.get('candidate', 0)}", "gr:list:candidate")],
        [("🏆 Рейтинг вступивших", "gr:list:joined"), ("🔎 Поисковые запросы", "gq:list")],
    ]))


@router.callback_query(F.data == "gr:bot")
async def bot_groups(cb: CallbackQuery) -> None:
    async with Session() as s:
        groups = (await s.scalars(select(Group).where(
            Group.status == "joined", Group.account_id.is_(None), Group.kind == "group")
            .order_by(Group.leads_count.desc()).limit(30))).all()
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
        return await review(cb)
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


# ---- группы бота модерации (tg-group-bot работает «разведчиком») ----
@router.callback_query(F.data == "fd:list")
async def feed_list(cb: CallbackQuery) -> None:
    async with Session() as s:
        chats = (await s.scalars(select(FeedChat).order_by(FeedChat.own.desc(), FeedChat.title))).all()
    if not chats:
        return await reply(cb, (
            "🛡 Бот модерации ещё не подключён к CRM.\n\n"
            "Нужно задать один и тот же секрет: FEED_TOKEN в .env CRM-бота и CRM_FEED_TOKEN "
            "(+ CRM_FEED_URL=http://crm:8090) в .env бота модерации, затем перезапустить оба."
        ), kb([[("◀️ Назад", "gr:menu")]]))
    rows = [[(("✅ " if c.enabled else "⬜ ") + c.title[:40] + (" · ваша" if c.own else " · клиента"),
              f"fd:tog:{c.tg_id}")] for c in chats[:40]]
    rows.append([("◀️ Назад", "gr:menu")])
    on = sum(c.enabled for c in chats)
    await reply(cb, (
        f"🛡 Группы бота модерации: {len(chats)}, передают заявки: {on}.\n"
        "Нажмите на группу, чтобы включить/выключить. Сообщения уходят в CRM только из ✅ групп.\n\n"
        "⚠️ Группы <b>клиентов</b> включайте только с согласия их владельцев."
    ), kb(rows))


@router.callback_query(F.data.startswith("fd:tog:"))
async def feed_toggle(cb: CallbackQuery) -> None:
    async with Session() as s:
        c = await s.get(FeedChat, int(cb.data.split(":")[2]))
        if c is None:
            return await cb.answer("Группа не найдена")
        c.enabled = not c.enabled
        await s.commit()
        warn = c.enabled and not c.own
    if warn:
        await cb.answer("Это группа клиента — убедитесь, что владелец согласен.", show_alert=True)
    await feed_list(cb)


# ---------- поиск: остановка и пауза ----------
@router.callback_query(F.data == "gr:stop")
async def stop_search(cb: CallbackQuery) -> None:
    from app.scout.search import request_stop

    request_stop()
    await cb.answer("Поиск остановится через несколько секунд", show_alert=True)


@router.callback_query(F.data == "gr:pause")
async def toggle_pause(cb: CallbackQuery) -> None:
    from app.scout.search import request_stop

    async with Session() as s:
        paused = await settings.get(s, "scout_paused") == "1"
        await settings.put(s, "scout_paused", "0" if paused else "1")
    if not paused:
        request_stop()
    await cb.answer("Разведчик продолжает работу" if paused else
                    "Разведчик на паузе: не ищет и не вступает в группы (заявки из групп приходят)", show_alert=True)
    await groups_menu(cb)


# ---------- отбор найденных групп списком ----------
async def review(target) -> None:
    async with Session() as s:
        total = await s.scalar(select(func.count()).select_from(Group).where(Group.status == "candidate"))
        groups = (await s.scalars(select(Group).where(Group.status == "candidate")
                                  .order_by(Group.members.desc()).limit(REVIEW_PAGE))).all()
    if not groups:
        return await reply(target, "❓ Новых групп для отбора нет.", kb([[("◀️ Назад", "gr:menu")]]))
    lines = []
    rows = []
    for i, g in enumerate(groups, 1):
        act = "н/д" if g.msgs_per_day < 0 else f"{g.msgs_per_day:.0f}"
        uname = f" @{esc(g.username)}" if g.username else ""
        lines.append(f"{i}. <b>{esc(g.title[:50])}</b>{uname} — {g.members} уч., {act} сообщ./сутки")
        rows.append([(f"✅ {i}. {g.title[:28]}", f"rv:j:{g.id}"), (f"❌ {i}", f"rv:s:{g.id}")])
    rows.append([("✅ Вступить во все", "rv:all:j"), ("❌ Пропустить все", "rv:all:s")])
    rows.append([("◀️ Назад", "gr:menu")])
    await reply(target, f"❓ <b>Отбор групп</b> (осталось {total})\n✅ — разведчик вступит, ❌ — пропустить.\n\n"
                + "\n".join(lines), kb(rows))


@router.callback_query(F.data == "rv:page")
async def review_page(cb: CallbackQuery) -> None:
    await review(cb)


@router.callback_query(F.data.regexp(r"^rv:(j|s):\d+$"))
async def review_one(cb: CallbackQuery) -> None:
    _, action, gid = cb.data.split(":")
    async with Session() as s:
        g = await s.get(Group, int(gid))
        if g and g.status == "candidate":
            g.status = "approved" if action == "j" else "skipped"
            await s.commit()
    await review(cb)


@router.callback_query(F.data.regexp(r"^rv:all:(j|s)$"))
async def review_all(cb: CallbackQuery) -> None:
    action = cb.data.split(":")[2]
    async with Session() as s:
        ids = (await s.scalars(select(Group.id).where(Group.status == "candidate")
                               .order_by(Group.members.desc()).limit(REVIEW_PAGE))).all()
        await s.execute(Group.__table__.update().where(Group.id.in_(ids))
                        .values(status="approved" if action == "j" else "skipped"))
        await s.commit()
    await review(cb)
