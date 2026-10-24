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
    "skipped": "пропущены", "left": "вышли",
}
REVIEW_PAGE = 8
BACKABLE = {"skipped": "🚫 Пропущены", "left": "👋 Вышли (ротация или вручную)"}


@router.message(F.text == "🔍 Группы")
async def groups_menu(target: Message | CallbackQuery) -> None:
    async with Session() as s:
        cnt = dict((await s.execute(select(Group.status, func.count()).group_by(Group.status))).all())
        bot_groups = await s.scalar(select(func.count()).select_from(Group).where(
            Group.status == "joined", Group.account_id.is_(None), Group.kind == "group"))
        pending = await catalog.pending_count(s)
        paused = await settings.get(s, "scout_paused") == "1"
        junk_n = await s.scalar(select(func.count()).select_from(Group).where(Group.status == "joined", Group.no_leads))
        from app.scout import captchas

        cap_n = await captchas.count(s)
    from app.bot.handlers import adposts as adposts_h

    ad_orders, ad_groups = await adposts_h.counts()
    me = await target.bot.me()
    privacy = "" if me.can_read_all_group_messages else (
        "\n⚠️ Privacy mode включён — бот не видит сообщения в группах, где он не админ. "
        "@BotFather → /setprivacy → Disable."
    )
    async with Session() as s:
        _, diag = await scoutdiag.join_diagnosis(s, manager)
    text = (
        f"🤖 Бот в группах: {bot_groups}{privacy}\n"
        f"🔍 Разведчик{' (⏸ на паузе)' if paused else ''}: "
        + ", ".join(f"{v} {cnt.get(k, 0)}" for k, v in STATUS.items())
        + "\n\n<b>Вступление в новые группы:</b>\n" + "\n".join(esc(x) for x in diag)
    )
    await reply(target, text, kb([
        [("➕ Добавить бота в группу", f"url:https://t.me/{me.username}?startgroup=true")],
        [("🤖 Группы бота", "gr:bot"), ("❓ Как это работает", "gr:help")],
        [("🛡 Бот модерации", "fd:list"), (f"📝 От пользователей {pending}", "up:list")],
        [("▶️ Искать сейчас", "gr:search"), ("⏹ Остановить поиск", "gr:stop")],
        [("▶️ Возобновить разведчика" if paused else "⏸ Пауза разведчика", "gr:pause"),
         ("🔄 Ротация сейчас", "gr:rotate")],
        [(f"⏳ Очередь {cnt.get('approved', 0)}", "gr:list:approved"), (f"❓ Кандидаты {cnt.get('candidate', 0)}", "gr:list:candidate")],
        [(f"🕓 Ждут одобрения {cnt.get('requested', 0)}", "gr:list:requested"), ("🏆 Рейтинг вступивших", "gr:list:joined")],
        [(f"🚫 Пропущены {cnt.get('skipped', 0)}", "gr:list:skipped"), (f"👋 Вышли {cnt.get('left', 0)}", "gr:list:left")],
        [(f"🛡 С нашим ботом {cnt.get('covered', 0)}", "gr:list:covered"), ("🔎 Поисковые запросы", "gq:list")],
        [(f"🙈 Без заявок (реклама/флуд) {junk_n}", "gr:junk"), ("📋 Правила групп", "gr:rules")],
        [("🗺 Города и регионы", "gr:geo"), ("🧹 Проверка групп", "gr:audit")],
        [(f"🧩 Капча — пройти вручную {cap_n}", "gr:cap")],
        [(f"📣 Заказы рекламы {ad_orders}", "apa:list"), (f"📢 Группы для рекламы {ad_groups}", "apg:list")],
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
        await cb.message.answer(*_captcha_card(g, days, note), disable_web_page_preview=True)


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
    admin = []
    if admin_text:
        admin.append(("📋 Текст админу", f"copy:{admin_text}"))
    if g.rules_contact:
        admin.append((f"✍️ @{g.rules_contact}"[:30], f"url:https://t.me/{g.rules_contact}"))
    if admin:
        rows.append(admin)
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
