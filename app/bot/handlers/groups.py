from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select

from app.bot.common import ask, esc, kb, reply
from app.db import Session
from app.models import FeedChat, Group, SearchQuery
from app.services import catalog, scoutdiag, settings

router = Router()
# Менеджер разведчиков подставляется из main.py
manager = None
STATUS = {
    "candidate": "кандидаты", "approved": "очередь", "requested": "ждут одобрения админа", "joined": "вступили", "covered": "уже с нашим ботом",
    "skipped": "пропущены", "left": "вышли", "manual": "вступить вручную",
}
REVIEW_PAGE = 8
BACKABLE = {"skipped": "🚫 Пропущены", "left": "👋 Вышли (ротация или вручную)"}


@router.message(F.text == "🔍 Группы")
async def _menu_counts(bot) -> dict:
    async with Session() as s:
        cnt = dict((await s.execute(select(Group.status, func.count()).group_by(Group.status))).all())
        c = {
            "cnt": cnt,
            "bot_groups": await s.scalar(select(func.count()).select_from(Group).where(
                Group.status == "joined", Group.account_id.is_(None), Group.kind == "group")),
            "pending": await catalog.pending_count(s),
            "paused": await settings.get(s, "scout_paused") == "1",
            "junk": await s.scalar(select(func.count()).select_from(Group).where(Group.status == "joined", Group.no_leads)),
        }
        from app.scout import captchas
        from app.services import botoffer

        c["cap"] = await captchas.count(s)
        c["offer"] = (await botoffer.counts(s)).get("", 0)
        _, c["diag"] = await scoutdiag.join_diagnosis(s, manager)
    from app.bot.handlers import adposts as adposts_h

    c["ad_orders"], c["ad_groups"] = await adposts_h.counts()
    c["me"] = await bot.me()
    return c


def _sections(c: dict) -> dict[str, tuple[str, list]]:
    """Подразделы «🔍 Группы» (раньше — 27 кнопок одним экраном): название и кнопки."""
    cnt = c["cnt"]
    return {
        "scout": ("🔍 Поиск и вступление", [
            [("▶️ Искать сейчас", "gr:search"), ("⏹ Остановить поиск", "gr:stop")],
            [("▶️ Возобновить разведчика" if c["paused"] else "⏸ Пауза разведчика", "gr:pause"),
             ("🔄 Ротация сейчас", "gr:rotate")],
            [(f"⏳ Очередь {cnt.get('approved', 0)}", "gr:list:approved"),
             (f"❓ Кандидаты {cnt.get('candidate', 0)}", "gr:list:candidate")],
            [(f"🕓 Ждут одобрения {cnt.get('requested', 0)}", "gr:list:requested"),
             (f"🏆 Вступили {cnt.get('joined', 0)}", "gr:list:joined")],
            [(f"🚫 Пропущены {cnt.get('skipped', 0)}", "gr:list:skipped"), (f"👋 Вышли {cnt.get('left', 0)}", "gr:list:left")],
            [(f"🛡 С нашим ботом {cnt.get('covered', 0)}", "gr:list:covered"), ("🔎 Поисковые запросы", "gq:list")],
            [(f"🖐 Вступить вручную {cnt.get('manual', 0)}", "gr:man"), (f"🧩 Капча {c['cap']}", "gr:cap")],
        ]),
        "quality": ("🧹 Качество групп", [
            [("🧹 Проверка групп", "gr:audit"), ("📋 Правила групп", "gr:rules")],
            [("🗺 Города и регионы", "gr:geo"), (f"🙈 Без заявок (реклама/флуд) {c['junk']}", "gr:junk")],
            [("🧹 Навести порядок", "gt:menu"), ("📍 Места разведчика", "gp:menu")],
        ]),
        "bot": ("🤖 Наш бот в группах", [
            [("➕ Добавить бота в группу", f"url:https://t.me/{c['me'].username}?startgroup=rating&admin=pin_messages")],
            [(f"🤖 Группы бота {c['bot_groups']}", "gr:bot"), ("📊 Рейтинг в группах", "gr:brd")],
            [(f"🤝 Предложить бота админам {c['offer']}", "gr:bo"), ("🛡 Бот модерации", "fd:list")],
            [(f"📝 От пользователей {c['pending']}", "up:list")],
        ]),
        "ads": ("📣 Реклама в группах", [
            [(f"📣 Заказы рекламы {c['ad_orders']}", "apa:list"), (f"📢 Группы для рекламы {c['ad_groups']}", "apg:list")],
        ]),
    }


async def groups_menu(target: Message | CallbackQuery) -> None:
    c = await _menu_counts(target.bot)
    cnt = c["cnt"]
    privacy = "" if c["me"].can_read_all_group_messages else (
        "\n⚠️ Privacy mode включён — бот не видит сообщения в группах, где он не админ. "
        "@BotFather → /setprivacy → Disable."
    )
    # что ждёт действий владельца — сверху, только если есть
    todo = [(label, data) for n, label, data in (
        (c["pending"], f"📝 Группы от пользователей · {c['pending']}", "up:list"),
        (cnt.get("manual", 0), f"🖐 Вступить вручную · {cnt.get('manual', 0)}", "gr:man"),
        (c["cap"], f"🧩 Капча · {c['cap']}", "gr:cap"),
        (cnt.get("candidate", 0), f"❓ Кандидаты · {cnt.get('candidate', 0)}", "gr:list:candidate"),
        (c["offer"], f"🤝 Предложить бота · {c['offer']}", "gr:bo"),
        (c["ad_orders"], f"📣 Заказы рекламы · {c['ad_orders']}", "apa:list"),
    ) if n]
    text = (
        f"🔍 <b>Группы</b>{' · ⏸ разведчик на паузе' if c['paused'] else ''}\n"
        f"Разведчик состоит в {cnt.get('joined', 0)}, в очереди {cnt.get('approved', 0)}; наш бот в группах: {c['bot_groups']}"
        f"{privacy}\n\n<b>Вступление в новые группы:</b>\n" + "\n".join(esc(x) for x in c["diag"])
        + ("\n\n⚡ <b>Ждут ваших действий</b> — кнопки сверху." if todo else "\n\n✅ Срочных дел нет.")
    )
    rows = [todo[i:i + 2] for i in range(0, len(todo), 2)]
    rows += [[("🔍 Поиск и вступление", "gr:sub:scout"), ("🧹 Качество групп", "gr:sub:quality")],
             [("🤖 Наш бот в группах", "gr:sub:bot"), ("📣 Реклама", "gr:sub:ads")],
             [("❓ Как это работает", "gr:help")]]
    await reply(target, text, kb(rows))


@router.callback_query(F.data.regexp(r"^gr:sub:(scout|quality|bot|ads)$"))
async def groups_section(cb: CallbackQuery) -> None:
    c = await _menu_counts(cb.bot)
    title, rows = _sections(c)[cb.data.split(":")[2]]
    await reply(cb, f"<b>{title}</b>", kb(rows + [[("◀️ Группы", "gr:menu")]]))


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
        "3. Бот молча читает сообщения и присылает вам заявки. В группе он пишет только одно — рейтинг заявок "
        "в закрепе, и то если админ дал ему право закреплять (выключить: /rating off в группе).\n\n"
        "Где взять группы: ваши группы, группы знакомых, а также админы, которым нужен бот-антиспам."
    ), kb([[("◀️ Назад", "gr:menu")]]))


@router.callback_query(F.data.startswith("gr:list:"))
async def group_list(cb: CallbackQuery) -> None:
    await show_list(cb, cb.data.split(":")[2])


async def show_list(cb: CallbackQuery, status: str) -> None:
    async with Session() as s:
        q = select(Group).where(Group.status == status)
        q = q.order_by(Group.leads_count.desc() if status == "joined" else Group.members.desc()).limit(15)
        groups = (await s.scalars(q)).all()
    if not groups:
        return await cb.answer("Пусто", show_alert=True)
    if status == "candidate":
        return await review(cb)
    if status in BACKABLE:  # пропущенные и покинутые можно вернуть в очередь по одной
        rows = [[(f"↩️ {g.title[:40]} · {g.members} уч." + (f" · {g.skip_reason[:30]}" if g.skip_reason else ""),
                  f"gr:back:{g.id}")] for g in groups if g.kind == "group"]
        rows.append([("◀️ Назад", "gr:menu")])
        return await reply(cb, f"{BACKABLE[status]} (нажмите на группу, чтобы вернуть её в очередь на вступление; "
                           "показаны самые большие):", kb(rows))
    lines = [f"• {esc(g.title)} — {g.members} уч., заявок: {g.leads_count}"
             + (f", заявка на вступление отправлена {g.join_attempts} р., повтор {g.join_retry_at:%d.%m}"
                if status == "requested" and g.join_retry_at else "") for g in groups]
    await reply(cb, f"{STATUS[status].capitalize()}:\n" + "\n".join(lines), kb([[("◀️ Назад", "gr:menu")]]))


@router.callback_query(F.data.startswith("gr:back:"))
async def back_to_queue(cb: CallbackQuery) -> None:
    async with Session() as s:
        g = await s.get(Group, int(cb.data.split(":")[2]))
        if g is None or g.status not in BACKABLE:
            return await cb.answer("Группа уже не в этом списке", show_alert=True)
        was = g.status
        g.status, g.join_attempts, g.join_retry_at = "approved", 0, None
        if g.skip_reason:  # админ сам вернул — проверку перед вступлением для неё не делаем
            g.skip_reason = "вернул админ"
        await s.commit()
    await cb.answer("Вернули в очередь на вступление")
    await show_list(cb, was)


@router.callback_query(F.data == "gr:rules")
async def rules_menu(cb: CallbackQuery) -> None:
    from app.services import passport

    async with Session() as s:
        cnt = await passport.counts(s)
        on = await settings.get(s, "group_rules_check") == "1"
        from app.services import ai, llm

        which = await ai.provider(s)
    rows = [[(f"{icon} {label} · {cnt.get(code, 0)}", f"gr:rl:{code}")] for code, (icon, label) in passport.POLICIES.items()]
    rows.append([("🔄 Прочитать следующие сейчас", "gr:rulesnow"), ("📨 Сводка сейчас", "gr:rulesdig")])
    rows.append([("⏸ Выключить чтение правил" if on else "▶️ Включить чтение правил", "gr:tg:group_rules_check")])
    rows.append([("◀️ Назад", "gr:menu")])
    await reply(cb, (
        "📋 <b>Правила групп</b> — разведчик читает описание, закреп, ботов и админов каждой своей группы и решает, "
        "можно ли там рекламу и к кому обращаться."
        + (f" Читает ИИ: {esc(llm.NAMES.get(which, which))}." if which
           else " ИИ не подключён — читаю по словам (менее точно).")
        + f"\nЕщё не прочитано: {cnt.get('', 0)}." + ("" if on else "\n⏸ Чтение выключено — кнопка «▶️ Включить» ниже.")
        + "\n\nАдминам бот сам не пишет (иначе аккаунт разведчика ограничат) — в списке «через админа» "
        "есть готовый текст и кнопка, отправляете одним нажатием."), kb(rows))


@router.callback_query(F.data.regexp(r"^gr:rl:(allowed|only_admin|paid|forbidden|unknown)$"))
async def rules_list(cb: CallbackQuery) -> None:
    from app.services import passport

    code = cb.data.split(":")[2]
    icon, label = passport.POLICIES[code]
    async with Session() as s:
        groups_ = await passport.listing(s, code)
    if not groups_:
        return await cb.answer("Пусто", show_alert=True)
    await cb.answer()
    await cb.message.answer(f"{icon} <b>{label}</b> (самые большие, до 15):")
    for g in groups_:
        text = (f"<b>{esc(g.title)}</b> · {g.members} уч." + (f" · @{esc(g.username)}" if g.username else "")
                + (f"\n{esc(g.rules_summary)}" if g.rules_summary else "")
                + (f"\n👤 По рекламе: @{esc(g.rules_contact)}" if g.rules_contact else "")
                + ("\n📢 открыта для рекламы" if g.ads_allowed else ""))
        rows = []
        if code in ("only_admin", "paid", "unknown") and g.rules_contact:
            rows.append([(f"✍️ Написать @{g.rules_contact}"[:40], f"url:https://t.me/{g.rules_contact}"),
                         ("📋 Текст для админа", f"copy:{passport.ready_message(g)}")])
        rows.append([("📢 Карточка для рекламы", f"apg:o:{g.id}")])
        await cb.message.answer(text, reply_markup=kb(rows), disable_web_page_preview=True)


@router.callback_query(F.data == "gr:rulesnow")
async def rules_now(cb: CallbackQuery) -> None:
    import asyncio

    from app.scout.publisher import rules_round

    if not manager or not manager.clients:
        return await cb.answer("Нет подключённых разведчиков", show_alert=True)
    await cb.answer("Читаю правила следующих групп — итог появится в списках через минуту", show_alert=True)
    asyncio.create_task(rules_round(manager))


@router.callback_query(F.data == "gr:rulesdig")
async def rules_digest(cb: CallbackQuery) -> None:
    from app.services import passport

    async with Session() as s:
        text = await passport.digest(s, force=True)
        await s.commit()
    await reply(cb, text or "Пока нечего сообщить.", kb([[("📋 Правила групп", "gr:rules")]]))


@router.callback_query(F.data == "gr:junk")
async def junk_list(cb: CallbackQuery) -> None:
    async with Session() as s:
        groups = (await s.scalars(select(Group).where(Group.status == "joined", Group.no_leads)
                                  .order_by(Group.members.desc()).limit(30))).all()
    rows = [[(f"✅ {g.title[:40]}", f"gr:junkoff:{g.id}")] for g in groups]
    rows.append([("◀️ Назад", "gr:menu")])
    await reply(cb, (
        "🙈 <b>Группы без заявок</b> — рекламные свалки, взаимопиар, флуды и ролевые: оттуда шёл только мусор.\n"
        "Разведчик в них остаётся (для рекламы), но заявки оттуда не создаются. Новые такие группы поиск пропускает.\n\n"
        + ("Нажмите на группу, чтобы снова брать из неё заявки." if groups else "Пока таких групп нет.")
        + "\nОтметить группу: под мусорной заявкой кнопка «🗑 Не брать из этой группы»."), kb(rows))


@router.callback_query(F.data.regexp(r"^gr:junkoff:\d+$"))
async def junk_off(cb: CallbackQuery) -> None:
    async with Session() as s:
        g = await s.get(Group, int(cb.data.split(":")[2]))
        if g is None:
            return await cb.answer("Группа не найдена", show_alert=True)
        g.no_leads = False
        await s.commit()
    await cb.answer("Снова берём заявки из этой группы")
    await junk_list(cb)


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
    await cb.answer("Поиск запущен — итог пришлю сообщением")
    async def run(client):
        try:
            await discover(client, report=True)
        except ConnectionError:
            pass  # о связи уже написали сообщением; фоновый поиск повторит сам

    for client in manager.clients.values():
        asyncio.create_task(run(client))


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
        from app.models import UserSearch

        users_n = await s.scalar(select(func.count()).select_from(UserSearch)) or 0
    rows = [[(("✅ " if q.enabled else "⏸ ") + q.text, f"gq:tog:{q.id}"), ("🗑", f"gq:del:{q.id}")] for q in qs]
    rows.append([("➕ Добавить", "gq:add"), ("◀️ Назад", "gr:menu")])
    rows.append([(f"🙋 Запросы пользователей {users_n}", "ms:all")])
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


# ---------- 🗺 Города и регионы, 🧹 Проверка групп (services/groupcheck.py) ----------
SCOPE_CODES = ("city", "region", "country", "none")


@router.callback_query(F.data == "gr:geo")
async def geo_menu(cb: CallbackQuery) -> None:
    from app.services import groupcheck

    async with Session() as s:
        cnt = await groupcheck.scope_counts(s)
        cities = await groupcheck.places(s, "city")
        regions = await groupcheck.places(s, "region")
    lines = ["🗺 <b>Группы разведчика по охвату</b> (вступившие):"]
    for code, (icon, label) in groupcheck.SCOPES.items():
        lines.append(f"{icon} {label}: {cnt.get(code, 0)}")
    if cities:
        lines.append("\n🏙 Города: " + ", ".join(f"{esc(p)} {n}" for p, n in cities))
    if regions:
        lines.append("🗺 Регионы: " + ", ".join(f"{esc(p)} {n}" for p, n in regions))
    lines.append("\nОхват определяется по названию, описанию и сообщениям (и ИИ, если подключён) — при поиске, "
                 "перед вступлением и при перепроверке. Ошибся — откройте группу в списке и поправьте.")
    rows = [[(f"{icon} {label} · {cnt.get(code, 0)}", f"gr:gs:{code or 'none'}")]
            for code, (icon, label) in groupcheck.SCOPES.items()]
    rows.append([("◀️ Назад", "gr:menu")])
    await reply(cb, "\n".join(lines), kb(rows))


@router.callback_query(F.data.regexp(r"^gr:gs:(city|region|country|none)$"))
async def geo_list(cb: CallbackQuery) -> None:
    code = cb.data.split(":")[2]
    scope = "" if code == "none" else code
    async with Session() as s:
        groups_ = (await s.scalars(select(Group).where(
            Group.status == "joined", Group.kind == "group", Group.scope == scope)
            .order_by(Group.members.desc()).limit(20))).all()
    if not groups_:
        return await cb.answer("Пусто", show_alert=True)
    rows = [[(f"{g.title[:34]} · {(g.city or g.region or '—')[:16]}", f"gr:pl:{g.id}")] for g in groups_]
    rows.append([("◀️ Назад", "gr:geo")])
    await reply(cb, "Нажмите на группу, чтобы поправить охват (показаны самые большие, до 20):", kb(rows))


def _place_card(g: Group) -> tuple[str, list]:
    from app.services import groupcheck

    icon, label = groupcheck.SCOPES.get(g.scope or "", groupcheck.SCOPES[""])
    text = (f"<b>{esc(g.title)}</b> · {g.members} уч.\n{icon} Охват: {label}"
            + (f"\n🏙 Город: {esc(g.city)}" if g.city else "") + (f"\n🗺 Регион: {esc(g.region)}" if g.region else "")
            + (f"\n🧹 Проверка: {esc(g.audit_verdict)}" + (f" ({g.audit_at:%d.%m})" if g.audit_at else "") if g.audit_verdict else ""))
    rows = [[("🏙 Город", f"gr:ps:{g.id}:city"), ("🗺 Регион", f"gr:ps:{g.id}:region"),
             ("🇷🇺 Вся страна", f"gr:ps:{g.id}:country")],
            [("✏️ Указать город/регион", f"gr:pe:{g.id}"), ("❓ Сбросить", f"gr:ps:{g.id}:none")],
            [("◀️ Назад", f"gr:gs:{g.scope or 'none'}")]]
    return text, rows


@router.callback_query(F.data.regexp(r"^gr:pl:\d+$"))
async def place_card(cb: CallbackQuery) -> None:
    async with Session() as s:
        g = await s.get(Group, int(cb.data.split(":")[2]))
    if g is None:
        return await cb.answer("Группа не найдена", show_alert=True)
    text, rows = _place_card(g)
    await reply(cb, text, kb(rows))


@router.callback_query(F.data.regexp(r"^gr:ps:\d+:(city|region|country|none)$"))
async def place_set(cb: CallbackQuery) -> None:
    _, _, gid, code = cb.data.split(":")
    async with Session() as s:
        g = await s.get(Group, int(gid))
        if g is None:
            return await cb.answer("Группа не найдена", show_alert=True)
        g.scope = "" if code == "none" else code
        if code in ("country", "none"):
            g.region = ""
            if not g.catalog:
                g.city = ""
        elif code == "region" and not g.catalog:
            g.city = ""
        await s.commit()
        text, rows = _place_card(g)
    await cb.answer("Сохранено")
    await reply(cb, text, kb(rows))


@router.callback_query(F.data.regexp(r"^gr:pe:\d+$"))
async def place_edit(cb: CallbackQuery, state: FSMContext) -> None:
    await ask(cb, state, "gr_place", "Напишите город или регион группы (например «Махачкала» или «Дагестан»). "
              "«-» — очистить.", group_id=int(cb.data.split(":")[2]))


async def on_place(s, m: Message, d: dict, text: str) -> bool:
    from app.services import geo

    g = await s.get(Group, d.get("group_id", 0))
    if g is None:
        await m.answer("Группа не найдена")
        return True
    value = text.strip()[:64]
    if value == "-":
        g.city, g.region, g.scope = "", "", ""
    else:
        city, region = geo.find(value)
        if city:
            g.city, g.region, g.scope = city, region, "city"
        elif region or value in geo.REGIONS:
            g.city, g.region, g.scope = "", region or value, "region"
        elif value.lower().endswith(("область", "край", "республика", "округ")):
            g.city, g.region, g.scope = "", value, "region"
        else:
            g.city, g.region, g.scope = value, g.region, "city"
    await s.commit()
    body, rows = _place_card(g)
    await m.answer(body, reply_markup=kb(rows))
    return True


@router.callback_query(F.data == "gr:audit")
async def audit_menu(cb: CallbackQuery) -> None:
    from datetime import timedelta

    from app.models import now
    from app.services import ai, groupcheck, llm

    async with Session() as s:
        week = await groupcheck.audit_stats(s, now() - timedelta(days=7))
        total = await s.scalar(select(func.count()).select_from(Group).where(
            Group.status == "joined", Group.kind == "group", Group.account_id.is_not(None))) or 0
        never = await s.scalar(select(func.count()).select_from(Group).where(
            Group.status == "joined", Group.kind == "group", Group.account_id.is_not(None), Group.audit_at.is_(None))) or 0
        on = await settings.get(s, "group_audit") == "1"
        leave = await settings.get(s, "group_audit_leave") == "1"
        which = await ai.provider(s)
    text = (
        "🧹 <b>Проверка групп</b> — разведчик перечитывает последние сообщения своих групп и решает: живая группа "
        "(объявления, просьбы об услугах) или спам, «заработок», реклама. Заодно определяет город/регион/всю страну.\n"
        + (f"Решает: правила + ИИ {esc(llm.NAMES.get(which, which))}." if which else "Решает: правила (ИИ не подключён).")
        + f"\n\nГрупп разведчика: {total}, ещё не проверены: {never}."
        + f"\nЗа неделю: проверено {week['checked']}, вышли из спамных {week['left']}, "
          f"оставили, но без заявок {week['muted']}."
        + ("" if on else "\n⏸ Проверка выключена — кнопка «▶️ Включить проверку» ниже.")
        + ("\nИз спамных разведчик выходит (кроме каталога, рекламы и групп по просьбе пользователей)." if leave
           else "\nИз спамных не выходит — только перестаёт брать заявки.")
        + "\n\nВернуть группу: «👋 Вышли» (снова вступит) или «🙈 Без заявок».")
    await reply(cb, text, kb([
        [("🔄 Проверить следующие сейчас", "gr:auditnow")],
        [("⏸ Выключить проверку" if on else "▶️ Включить проверку", "gr:tg:group_audit"),
         ("🚪 Не выходить из спамных" if leave else "🚪 Выходить из спамных", "gr:tg:group_audit_leave")],
        [("👋 Вышли", "gr:list:left"), ("🙈 Без заявок", "gr:junk")],
        [("◀️ Назад", "gr:menu")]]))


@router.callback_query(F.data == "gr:auditnow")
async def audit_now(cb: CallbackQuery) -> None:
    import asyncio

    if not manager or not manager.clients:
        return await cb.answer("Нет подключённых разведчиков", show_alert=True)
    await cb.answer("Проверяю следующие 3 группы — итог сообщением через пару минут", show_alert=True)

    async def run() -> None:
        from app.scout.publisher import audit_one
        from app.services import groupcheck

        names = {"good": "хорошие", "left": "вышли (спам)", "muted": "без заявок", "skip": "не прочитались"}
        async with Session() as s:  # кнопка работает и при выключенной фоновой проверке
            ids = await groupcheck.due(s, set(manager.clients))
        res: dict[str, int] = {}
        for gid in ids:
            try:
                r = await audit_one(manager, gid)
            except Exception:
                r = "skip"
            res[r] = res.get(r, 0) + 1
        summary = ", ".join(f"{names.get(k, k)}: {n}" for k, n in res.items()) or "проверять нечего"
        await cb.message.answer(f"🧹 Проверка групп: {summary}.", reply_markup=kb([[("🧹 Проверка групп", "gr:audit")]]))

    asyncio.create_task(run())


TOGGLES = {"group_rules_check": "gr:rules", "group_audit": "gr:audit", "group_audit_leave": "gr:audit"}


@router.callback_query(F.data.regexp(r"^gr:tg:(group_rules_check|group_audit|group_audit_leave)$"))
async def toggle(cb: CallbackQuery) -> None:
    """Включить/выключить прямо из раздела (то же, что ⚙️ → 🔍 Разведчик)."""
    key = cb.data.split(":")[2]
    async with Session() as s:
        on = await settings.get(s, key) == "1"
        await settings.put(s, key, "0" if on else "1")
    # меню само ответит на нажатие (второй ответ Telegram не принимает) — новое состояние видно на кнопке
    await (rules_menu(cb) if TOGGLES[key] == "gr:rules" else audit_menu(cb))


# ---------- 🧩 Капча (scout/captchas.py): ссылки на сообщения ботов-модераторов, выход, если не прошли ----------
@router.callback_query(F.data == "gr:cap")
async def captcha_list(cb: CallbackQuery) -> None:
    from app.scout import captchas

    async with Session() as s:
        groups_ = await captchas.listing(s)
        days = await settings.get_int(s, "captcha_leave_days")
    head = ("🧩 <b>Капча</b> — в этих группах бот-модератор просит разведчика подтвердить, что он не бот. "
            "Пока не пройдёте, разведчик может только читать (заявки идут), писать нельзя.\n"
            "Откройте ссылку <b>с телефона разведчика</b> и нажмите кнопку проверки, затем «✅ Прошёл».\n"
            "ИИ капчу не проходит — Telegram за это блокирует аккаунт. Лучший выход — «📋 Текст админу»: "
            "предложить админу бесплатного бота-антиспама, тогда капча не нужна.\n"
            + (f"Не пройдёте за {days} дн. и заявок оттуда не будет — разведчик сам выйдет (⚙️ → 🧹 Проверка групп и капча)."
               if days else "Автовыход выключен (⚙️ → 🧹 Проверка групп и капча)."))
    if not groups_:
        return await reply(cb, head + "\n\nСейчас таких групп нет ✅", kb([[("◀️ Назад", "gr:menu")]]))
    await reply(cb, head + f"\n\nГрупп: {len(groups_)} (карточки ниже).", kb([[("◀️ Назад", "gr:menu")]]))
    from app.scout import captchas

    for g in groups_:
        async with Session() as s:
            note = await captchas.admin_text(s, g)
        text, markup = _captcha_card(g, days, note)
        await cb.message.answer(text, reply_markup=markup, disable_web_page_preview=True)


def _captcha_card(g: Group, days: int, admin_text: str = ""):
    from app.scout import captchas

    until = captchas.leave_at(g, days)
    leads = captchas.has_leads(g)
    text = (f"🧩 <b>{esc(g.title)}</b> · {g.members} уч.\nКапча с {g.captcha_since:%d.%m %H:%M}"
            + ("\n📥 Заявки отсюда идут — из группы не выхожу" if leads else (f", выйду {until:%d.%m}" if until else ""))
            + ("" if g.captcha_link else "\nСообщение бота не нашёл — откройте группу и найдите проверку."))
    url = g.captcha_link or captchas.group_url(g)
    rows = ([[("🧩 Открыть сообщение бота" if g.captcha_link else "🧩 Открыть группу", f"url:{url}")]] if url else [])
    rows += [[("✅ Прошёл — проверить", f"gr:capok:{g.id}"), ("⏳ Ещё время", f"gr:capwait:{g.id}")]]
    from app.services import botoffer

    who = botoffer.contact(g)
    if who and admin_text:  # чат с админом, где предложение уже вставлено — остаётся «Отправить»
        rows.append([(f"✉️ Предложить бота @{who}"[:40], f"url:{botoffer.link(who, admin_text)}")])
    elif admin_text:
        rows.append([("📋 Текст админу", f"copy:{admin_text}")])
    rows.append([("🚪 Выйти из группы", f"gr:capout:{g.id}")])
    return text, kb(rows)


@router.callback_query(F.data.regexp(r"^gr:capok:\d+$"))
async def captcha_ok(cb: CallbackQuery) -> None:
    from app.scout import captchas

    if not manager or not manager.clients:
        return await cb.answer("Нет подключённых разведчиков", show_alert=True)
    gid = int(cb.data.split(":")[2])
    async with Session() as s:  # проверить сразу, без ожидания часа
        g = await s.get(Group, gid)
        if g is not None:
            g.captcha_checked_at = None
            await s.commit()
    r = await captchas.check_one(manager, gid)
    msg = {"passed": "✅ Пройдено — разведчик снова может писать в группе", "ok": "✅ Ограничений нет",
           "still": "⏳ Ещё ограничен — проверка не пройдена. Откройте ссылку с телефона разведчика",
           "kicked": "Разведчика уже удалили из группы (она в «👋 Вышли», можно вернуть в очередь)",
           }.get(r, "Не получилось проверить, попробуйте позже")
    await cb.answer(msg, show_alert=True)
    if r in ("passed", "ok", "kicked"):
        try:
            await cb.message.delete()
        except Exception:
            pass


@router.callback_query(F.data.regexp(r"^gr:capwait:\d+$"))
async def captcha_wait(cb: CallbackQuery) -> None:
    from app.models import now

    async with Session() as s:
        g = await s.get(Group, int(cb.data.split(":")[2]))
        if g is None or g.captcha_since is None:
            return await cb.answer("Капчи здесь уже нет", show_alert=True)
        g.captcha_since = now()  # срок автовыхода отсчитывается заново
        days = await settings.get_int(s, "captcha_leave_days")
        await s.commit()
        from app.scout import captchas

        text, markup = _captcha_card(g, days, await captchas.admin_text(s, g))
    await cb.answer(f"Хорошо, подожду ещё {days} дн." if days else "Хорошо")
    try:
        await cb.message.edit_text(text, reply_markup=markup, disable_web_page_preview=True)
    except Exception:
        pass


@router.callback_query(F.data.regexp(r"^gr:capout:\d+$"))
async def captcha_out(cb: CallbackQuery) -> None:
    from app.scout import captchas

    async with Session() as s:
        g = await s.get(Group, int(cb.data.split(":")[2]))
        client = manager.clients.get(g.account_id) if (manager and g is not None) else None
        if g is None or g.status != "joined":
            return await cb.answer("Уже не в группе", show_alert=True)
        if client is None:
            return await cb.answer("Разведчик этой группы не подключён", show_alert=True)
        await s.commit()
        ok = await captchas.leave(client, g, "вышел вручную (капча)")
        await s.commit()
    await cb.answer("Вышел. Вернуть — 🔍 Группы → 👋 Вышли" if ok else "Не получилось выйти, попробуйте позже",
                    show_alert=True)
    if ok:
        try:
            await cb.message.delete()
        except Exception:
            pass


# ---------- 🖐 Вступить вручную: группы с ботом-капчей — вступает человек с телефона разведчика ----------
@router.callback_query(F.data == "gr:man")
async def manual_list(cb: CallbackQuery) -> None:
    from app.models import ScoutAccount

    async with Session() as s:
        groups_ = (await s.scalars(select(Group).where(Group.status == "manual")
                                   .order_by(Group.members.desc()).limit(20))).all()
        names = {a.id: a.name for a in (await s.scalars(select(ScoutAccount))).all()}
    head = ("🖐 <b>Вступить вручную</b> — в этих группах бот-капча выкидывает новичков, которые не прошли проверку. "
            "Разведчик сам туда не вступает (Telegram банит аккаунты, которые проходят капчу программой).\n"
            "Откройте группу <b>с телефона разведчика</b>, вступите и пройдите проверку, затем нажмите «✅ Вступил». "
            "Дальше разведчик читает группу как обычно.")
    if not groups_:
        return await reply(cb, head + "\n\nСейчас таких групп нет ✅", kb([[("◀️ Назад", "gr:menu")]]))
    await reply(cb, head + f"\n\nГрупп: {len(groups_)} (карточки ниже).", kb([[("◀️ Назад", "gr:menu")]]))
    from app.scout import captchas

    for g in groups_:
        who = names.get(g.account_id or 0, "")
        text = (f"🖐 <b>{esc(g.title)}</b> · {g.members} уч.\n{esc(g.skip_reason)}"
                + (f"\nВступать с аккаунта: {esc(who)}" if who else ""))
        rows = ([[("🔗 Открыть группу", f"url:{captchas.group_url(g)}")]] if captchas.group_url(g) else [])
        rows.append([("✅ Вступил — проверить", f"gr:mok:{g.id}"), ("🚫 Не нужна", f"gr:mno:{g.id}")])
        await cb.message.answer(text, reply_markup=kb(rows), disable_web_page_preview=True)


@router.callback_query(F.data.regexp(r"^gr:mok:\d+$"))
async def manual_ok(cb: CallbackQuery) -> None:
    from app.models import now
    from app.scout import captchas

    if not manager or not manager.clients:
        return await cb.answer("Нет подключённых разведчиков", show_alert=True)
    async with Session() as s:
        g = await s.get(Group, int(cb.data.split(":")[2]))
        if g is None or g.status != "manual":
            return await cb.answer("Группа уже не в этом списке", show_alert=True)
        await s.commit()  # не держим базу, пока спрашиваем Telegram
        acc_id, limited = await captchas.find_member(manager, g)
        if acc_id is None:
            return await cb.answer("Разведчик пока не в группе. Вступите с телефона разведчика и нажмите ещё раз.",
                                   show_alert=True)
        g.status, g.account_id, g.joined_at, g.skip_reason = "joined", acc_id, now(), ""
        g.captcha_since = now() if limited else None
        g.captcha_checked_at = now()
        await s.commit()
    await cb.answer("✅ Вступил — разведчик читает группу" + (
        ". Но писать пока нельзя — проверка не пройдена (группа в «🧩 Капча»)" if limited else ""), show_alert=True)
    try:
        await cb.message.delete()
    except Exception:
        pass


@router.callback_query(F.data.regexp(r"^gr:mno:\d+$"))
async def manual_no(cb: CallbackQuery) -> None:
    async with Session() as s:
        g = await s.get(Group, int(cb.data.split(":")[2]))
        if g is None or g.status != "manual":
            return await cb.answer("Группа уже не в этом списке", show_alert=True)
        g.status = "skipped"
        await s.commit()
    await cb.answer("Убрал в «🚫 Пропущены» — оттуда можно вернуть", show_alert=True)
    try:
        await cb.message.delete()
    except Exception:
        pass


# ---------- 🤝 Предложить бота админам (services/botoffer.py): пишет владелец сам, одним нажатием ----------
@router.callback_query(F.data == "gr:bo")
async def offer_menu(cb: CallbackQuery) -> None:
    from app.services import botoffer

    async with Session() as s:
        cnt = await botoffer.counts(s)
        groups_ = await botoffer.todo(s)
        bot = await settings.get(s, "mod_bot_username")
        texts = {g.id: await botoffer.text(s, g) for g in groups_}
    head = ("🤝 <b>Предложить бота админам</b> — группы разведчика, где известен админ (из правил группы). "
            "Нажмите «✉️ Написать»: откроется чат с админом, текст уже вставлен — остаётся «Отправить». "
            "Потом отметьте «✅ Отправил».\n"
            "Пишете вы со своего аккаунта: если писать незнакомым с аккаунта-разведчика, Telegram его ограничит "
            "и поиск заявок остановится.\n"
            + ("" if bot else "\n⚠️ Впишите имя вашего бота модерации: ⚙️ → 🧹 Проверка групп и капча → «Бот модерации (@)».\n")
            + f"\nНе предлагали: {cnt.get('', 0)}" + (" (первыми — группы с капчей)" if groups_ else ""))
    rows = [[(f"{label} · {cnt.get(code, 0)}", f"gr:bol:{code}")] for code, label in botoffer.STATUSES.items()]
    rows.append([("◀️ Назад", "gr:menu")])
    await reply(cb, head, kb(rows))
    for g in groups_:
        text, markup = _offer_card(g, texts[g.id])
        await cb.message.answer(text, reply_markup=markup, disable_web_page_preview=True)


def _offer_card(g: Group, message: str):
    from app.services import botoffer

    who = botoffer.contact(g)
    note = ("🧩 капча" if g.captcha_since else "🖐 нужно вступить вручную" if g.status == "manual" else "")
    text = (f"<b>{esc(g.title)}</b> · {g.members} уч." + (f" · {note}" if note else "")
            + (f"\n📋 {esc(g.rules_summary)}" if g.rules_summary else "")
            + (f"\nСтатус: {botoffer.STATUSES[g.offer_status]}" if g.offer_status in botoffer.STATUSES else ""))
    rows = [[(f"✉️ Написать @{who}"[:40], f"url:{botoffer.link(who, message)}")]] if who else []
    if g.offer_status == "":
        rows.append([("✅ Отправил", f"gr:bos:{g.id}:sent"), ("⏭ Не сейчас", f"gr:bos:{g.id}:skip")])
    elif g.offer_status == "sent":
        rows.append([("🛡 Поставили бота", f"gr:bos:{g.id}:bot"), ("🤝 Договорились о рекламе", f"gr:bos:{g.id}:ads")])
        rows.append([("👎 Отказ", f"gr:bos:{g.id}:no"), ("↩️ Не отправлял", f"gr:bos:{g.id}:")])
    else:
        rows.append([("↩️ Вернуть в «не предлагали»", f"gr:bos:{g.id}:")])
    return text, kb(rows)


@router.callback_query(F.data.regexp(r"^gr:bol:(sent|bot|ads|no|skip)$"))
async def offer_list(cb: CallbackQuery) -> None:
    from app.services import botoffer

    code = cb.data.split(":")[2]
    async with Session() as s:
        groups_ = await botoffer.listing(s, code)
        texts = {g.id: await botoffer.text(s, g) for g in groups_}
    if not groups_:
        return await cb.answer("Пусто", show_alert=True)
    await cb.answer()
    await cb.message.answer(f"{botoffer.STATUSES[code]} (последние, до 15):")
    for g in groups_:
        text, markup = _offer_card(g, texts[g.id])
        await cb.message.answer(text, reply_markup=markup, disable_web_page_preview=True)


@router.callback_query(F.data.regexp(r"^gr:bos:\d+:(sent|bot|ads|no|skip)?$"))
async def offer_set(cb: CallbackQuery) -> None:
    from app.services import botoffer

    _, _, gid, code = cb.data.split(":")
    async with Session() as s:
        g = await s.get(Group, int(gid))
        if g is None:
            return await cb.answer("Группа не найдена", show_alert=True)
        await botoffer.mark(s, g, code)
        await s.commit()
        message = await botoffer.text(s, g)
    await cb.answer({"sent": "Отметил: ждём ответа", "bot": "🛡 Отлично — бот в группе",
                     "ads": "🤝 Договорились — правила группы больше не мешают рекламе. Разрешить рекламу: «📢 Группы для рекламы»",
                     "no": "Отметил отказ", "skip": "Отложил", "": "Вернул в «не предлагали»"}[code],
                    show_alert=code == "ads")
    text, markup = _offer_card(g, message)
    try:
        await cb.message.edit_text(text, reply_markup=markup, disable_web_page_preview=True)
    except Exception:
        pass


# ---------- 📍 Места разведчика и 🧹 Навести порядок (владелец 25.10) ----------
def _place_buttons(allowed: list[str], page: str) -> list:
    from app.services import geo

    names = geo.REGIONS if page == "r" else list(geo.CITIES)
    btns = [(("✅ " if n in allowed else "⬜ ") + n, f"gp:t:{geo.KNOWN_PLACES.index(n) + 1}:{page}") for n in names if n in geo.KNOWN_PLACES]
    return [btns[i:i + 2] for i in range(0, len(btns), 2)]


async def places_view(target, page: str = "r") -> None:
    from app.services import geo, tidy

    async with Session() as s:
        allowed = geo.parse_places(await settings.get(s, "scout_places"))
        users = await tidy.user_places(s)
        home = await settings.get(s, "home_region")
    mine = ", ".join(f"{p} — {n}" for p, n in users[:8]) or "никто ещё не указал город"
    text = ("📍 <b>Места разведчика</b>\nРазведчик ищет и берёт только группы из выбранных мест. Группы, где место "
            "не определить, он пока берёт и после вступления проверяет.\n\n"
            f"Сейчас: <b>{esc(', '.join(allowed)) if allowed else 'любые (ограничения нет)'}</b>\n"
            f"👥 Места ваших пользователей: {esc(mine)}\nСвой регион: {esc(home or '—')}\n\n"
            "Отметьте нужные (регион включает его города):")
    rows = [[("📥 Взять места пользователей", "gp:users"), ("🌐 Любые места", "gp:any")]]
    rows += _place_buttons(allowed, page)
    rows.append([("🏙 Города" if page == "r" else "🗺 Регионы", f"gp:pg:{'c' if page == 'r' else 'r'}"), ("◀️ Назад", "gr:sub:quality")])
    await reply(target, text, kb(rows))


@router.callback_query(F.data == "gp:menu")
async def gp_menu(cb: CallbackQuery) -> None:
    await places_view(cb)


@router.callback_query(F.data.regexp(r"^gp:pg:[rc]$"))
async def gp_page(cb: CallbackQuery) -> None:
    await places_view(cb, cb.data.split(":")[2])


@router.callback_query(F.data.regexp(r"^gp:t:\d+:[rc]$"))
async def gp_toggle(cb: CallbackQuery) -> None:
    from app.services import geo

    _, _, idx, page = cb.data.split(":")
    i = int(idx) - 1
    if not 0 <= i < len(geo.KNOWN_PLACES):
        return await cb.answer("Нет такого места", show_alert=True)
    name = geo.KNOWN_PLACES[i]
    async with Session() as s:
        allowed = geo.parse_places(await settings.get(s, "scout_places"))
        allowed = [p for p in allowed if p != name] if name in allowed else allowed + [name]
        await settings.put(s, "scout_places", ", ".join(allowed))
    await places_view(cb, page)


@router.callback_query(F.data == "gp:users")
async def gp_users(cb: CallbackQuery) -> None:
    from app.services import tidy

    async with Session() as s:
        users = await tidy.user_places(s)
        allowed = tidy.allowed_default(users, await settings.get(s, "home_region"))
        await settings.put(s, "scout_places", ", ".join(allowed))
    await cb.answer(f"Выбрано мест: {len(allowed)}" if allowed else "Пользователи пока не указали города", show_alert=not allowed)
    await places_view(cb)


@router.callback_query(F.data == "gp:any")
async def gp_any(cb: CallbackQuery) -> None:
    async with Session() as s:
        await settings.put(s, "scout_places", "")
    await cb.answer("Ограничение по местам снято")
    await places_view(cb)


async def tidy_view(target) -> None:
    from app.services import tidy

    async with Session() as s:
        c = await tidy.classify(s)
        users = await tidy.user_places(s)
        grow = await settings.get(s, "scout_grow") == "1"
    allowed = c["allowed"]
    mine = ", ".join(f"{p} — {n}" for p, n in users[:6]) or "никто ещё не указал город"
    lines = ["🧹 <b>Навести порядок в группах</b>",
             f"Разведчик состоит в {c['total']} группах. Остаются: {c['keep']} подходящих + {c['protected']} защищённых "
             "(каталог, реклама, просьбы пользователей).",
             f"📍 Места: <b>{esc(', '.join(allowed)) if allowed else 'не выбраны — сначала «📍 Места разведчика»'}</b>",
             f"👥 Города пользователей: {esc(mine)}", ""]
    rows = []
    for kind, (title, why) in tidy.KINDS.items():
        n = len(c[kind])
        lines.append(f"{title}: <b>{n}</b> — {why}")
        for g in c[kind][:3]:
            lines.append(f"   • {esc(g.title[:40])}")
        if n:
            rows.append([(f"👀 {title.split(' ', 1)[1]} · {n}", f"gt:v:{kind}")])
    lines += ["", f"🔎 Поиск и вступление в новые группы: {'▶️ идёт' if grow else '⏹ остановлен'}"]
    rows.append([("⏹ Остановить поиск и вступление" if grow else "▶️ Включить поиск и вступление", "gt:grow")])
    rows.append([("📍 Места разведчика", "gp:menu"), ("◀️ Назад", "gr:sub:quality")])
    await reply(target, "\n".join(lines), kb(rows))


@router.callback_query(F.data == "gt:menu")
async def gt_menu(cb: CallbackQuery) -> None:
    await tidy_view(cb)


@router.callback_query(F.data == "gt:grow")
async def gt_grow(cb: CallbackQuery) -> None:
    async with Session() as s:
        on = await settings.get(s, "scout_grow") == "1"
        await settings.put(s, "scout_grow", "0" if on else "1")
    await cb.answer("Поиск и вступление остановлены" if on else "Поиск и вступление включены", show_alert=True)
    await tidy_view(cb)


@router.callback_query(F.data.regexp(r"^gt:v:(spam|place|quiet)$"))
async def gt_view(cb: CallbackQuery) -> None:
    from app.services import tidy

    kind = cb.data.split(":")[2]
    async with Session() as s:
        c = await tidy.classify(s)
    groups_ = c[kind]
    title, why = tidy.KINDS[kind]
    if not groups_:
        return await reply(cb, f"{title}: пусто ✅", kb([[("◀️ Назад", "gt:menu")]]))
    lines = [f"<b>{title}</b> — {len(groups_)} групп\n{why}\n"]
    for g in groups_[:25]:
        where = g.city or g.region or "место не определено"
        lines.append(f"• {esc(g.title[:45])} · {esc(where)} · заявок {g.leads_count or 0}")
    if len(groups_) > 25:
        lines.append(f"…и ещё {len(groups_) - 25}")
    lines.append("\nВыйти можно только отсюда; вернуть группу — 🔍 Группы → 🔍 Поиск и вступление → 👋 Вышли → ↩️.")
    await reply(cb, "\n".join(lines), kb([[(f"👋 Выйти из {len(groups_)} групп", f"gt:go:{kind}")], [("◀️ Назад", "gt:menu")]]))


@router.callback_query(F.data.regexp(r"^gt:go:(spam|place|quiet)$"))
async def gt_go(cb: CallbackQuery) -> None:
    import asyncio

    from app.bot import notify
    from app.scout import publisher
    from app.services import tidy

    kind = cb.data.split(":")[2]
    if manager is None:
        return await cb.answer("Разведчик сейчас не запущен", show_alert=True)
    if publisher._leaving.locked():
        return await cb.answer("Выход из групп уже идёт — дождитесь итога", show_alert=True)
    async with Session() as s:
        c = await tidy.classify(s)
    ids = [g.id for g in c[kind]]
    if not ids:
        return await cb.answer("Таких групп нет", show_alert=True)
    title = tidy.KINDS[kind][0]

    async def run():
        res = await publisher.leave_groups(manager, ids, f"порядок: {title.split(' ', 1)[1].lower()}")
        await notify._send(f"🧹 {title}: вышел из {res['left']} групп"
                           + (f", не вышло {res['failed']}" if res["failed"] else "")
                           + (f", пропущено {res['skipped']}" if res["skipped"] else "")
                           + ". Вернуть: 🔍 Группы → 🔍 Поиск и вступление → 👋 Вышли.")

    asyncio.create_task(run())
    await cb.answer(f"Выхожу из {len(ids)} групп — это займёт несколько минут, пришлю итог", show_alert=True)
    await tidy_view(cb)
