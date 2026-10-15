"""🌐 Каталог: пользователи добавляют открытые группы и каналы, смотрят каталог, получают бонусы."""
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select

from app.bot import notify
from app.bot.common import ask, esc, kb, reply
from app.db import Session
from app.models import Category, Group, User, UserCategory
from app.services import ads, catalog, geo, levels, settings

router = Router()

KIND = {"group": "👥", "channel": "📢"}
STATUS = {
    "draft": "не отправлена", "pending": "⏳ на проверке", "approved": "✅ в каталоге, скоро подключим", "requested": "✅ в каталоге, скоро подключим",
    "joined": "✅ в каталоге", "covered": "✅ в каталоге", "rejected": "❌ отклонена",
    "candidate": "✅ в каталоге", "skipped": "✅ в каталоге", "left": "✅ в каталоге",
}


@router.message(F.text == "🌐 Каталог")
async def catalog_menu(target: Message | CallbackQuery, user: User) -> None:
    async with Session() as s:
        per = await settings.get_int(s, "bonus_leads")
        groups_n, leads_n = await levels.stats(s, user.tg_id)
        from app.bot.handlers import adposts as adposts_h

        ad_rows = await adposts_h.menu_rows(s)
    level = levels.level_of(groups_n, leads_n)
    await reply(target, (
        "🌐 <b>Каталог групп</b>\n\n"
        "Здесь собраны открытые группы и каналы по нишам и городам. Добавьте свою группу или канал — "
        "мы подключим его к поиску заявок.\n\n"
        f"🎁 <b>Бонус:</b> за каждые {per} заявок, найденных в добавленной вами группе, — "
        "+1 день 💎 Полного тарифа.\n"
        "⚡ <b>Право первооткрывателя:</b> заявки из вашей группы всегда приходят вам — даже сверх дневного лимита.\n\n"
        f"Ваш уровень: <b>{levels.name(level)}</b> (заявок из ваших групп: {leads_n})\n{levels.next_step(level, leads_n)}"
    ), kb([
        [("📚 Каталог по моим нишам", "cg:browse:mine"), ("🌍 Весь каталог", "cg:browse:all")],
        [("➕ Добавить группу или канал", "cg:add")],
        [("📂 Мои группы и бонусы", "cg:my"), ("🏆 Рейтинг", "cg:top:mine")],
    ] + ad_rows))


@router.callback_query(F.data == "cg:menu")
async def back(cb: CallbackQuery, user: User) -> None:
    await catalog_menu(cb, user)


@router.callback_query(F.data.regexp(r"^cg:browse:(mine|all)$"))
async def browse(cb: CallbackQuery, user: User) -> None:
    everything = cb.data.endswith("all")
    async with Session() as s:
        roots = set((await s.scalars(select(UserCategory.category_id).where(UserCategory.user_id == user.tg_id))).all())
        groups = await catalog.browse(s, user, roots, everything)
    if not groups:
        return await reply(cb, "Пока пусто. Станьте первым — добавьте группу 👇",
                           kb([[("➕ Добавить группу или канал", "cg:add")], [("◀️ Назад", "cg:menu")]]))
    lines = [
        ("📢 " if ads.active(g) else "") + f"{KIND[g.kind]} <a href=\"https://t.me/{esc(g.username)}\">{esc(g.title[:50])}</a> — {g.members} уч."
        + (f", {esc(g.city)}" if g.city else "") + (f", заявок: {g.leads_count}" if g.leads_count else "")
        for g in groups
    ]
    head = "🌍 Весь каталог" if everything else "📚 Каталог по вашим нишам и городу"
    await reply(cb, f"{head}:\n\n" + "\n".join(lines), kb([[("◀️ Назад", "cg:menu")]]))


@router.callback_query(F.data == "cg:add")
async def add(cb: CallbackQuery, state: FSMContext) -> None:
    await ask(cb, state, "grp_link", (
        "Пришлите ссылку на <b>открытую</b> группу или канал: t.me/название или @название.\n"
        "Закрытые (по приглашению) не принимаем. Для канала должен быть включён веб-просмотр t.me/s/название."))


async def grp_link(s, m: Message, d, text):
    """Ссылка -> проверка -> черновик -> выбор категории."""
    name = catalog.parse_username(text)
    if name is None:
        return await m.answer("Не похоже на ссылку на открытую группу. Пример: t.me/mygroup или @mygroup") and False
    user = await s.get(User, m.from_user.id)
    try:
        info = await catalog.lookup(m.bot, name)
        if info.kind == "channel":
            from app.scout.channels import fetch_posts

            if not await fetch_posts(info.username):
                raise catalog.SubmitError(
                    "У канала не открывается веб-просмотр t.me/s/" + info.username +
                    " — читать его не получится. Включите его в настройках канала или пришлите другой.")
        g = await catalog.start_submission(s, user, info)
    except catalog.SubmitError as e:
        return await m.answer(f"⚠️ {esc(str(e))}")
    roots = (await s.scalars(select(Category).where(Category.parent_id.is_(None), Category.enabled).order_by(Category.id))).all()
    rows = [[(f"{c.emoji} {c.name}", f"cg:cat:{g.id}:{c.id}")] for c in roots]
    await m.answer(
        f"{KIND[g.kind]} <b>{esc(g.title)}</b> — {g.members} уч.\nК какой нише относится?",
        reply_markup=kb(rows), disable_web_page_preview=True)


async def grp_city(s, m: Message, d, text):
    g = await s.get(Group, d["gid"])
    if g is None or g.submitted_by != m.from_user.id or g.status != "draft":
        return await m.answer("Эта заявка уже обработана.")
    g.city = text.strip()[:64]
    await submit(m, s, g)


def _mine_draft(g: Group | None, user: User) -> bool:
    return g is not None and g.submitted_by == user.tg_id and g.status == "draft"


@router.callback_query(F.data.regexp(r"^cg:cat:\d+:\d+$"))
async def pick_category(cb: CallbackQuery, user: User) -> None:
    _, _, gid, cid = cb.data.split(":")
    async with Session() as s:
        g = await s.get(Group, int(gid))
        if not _mine_draft(g, user):
            return await cb.answer("Эта заявка уже обработана", show_alert=True)
        g.category_id = int(cid)
        await s.commit()
    rows = [[(c, f"cg:city:{gid}:{i}")] for i, c in enumerate(geo.MENU_CITIES)]
    rows.append([("🇷🇺 Для всей России", f"cg:city:{gid}:all"), ("✍️ Другой город", f"cg:city:{gid}:other")])
    await reply(cb, "📍 В каком городе (регионе) эта группа?", kb(rows))


@router.callback_query(F.data.regexp(r"^cg:city:\d+:(\d+|all|other)$"))
async def pick_city(cb: CallbackQuery, state: FSMContext, user: User) -> None:
    _, _, gid, value = cb.data.split(":")
    async with Session() as s:
        g = await s.get(Group, int(gid))
        if not _mine_draft(g, user):
            return await cb.answer("Эта заявка уже обработана", show_alert=True)
        if value == "other":
            return await ask(cb, state, "grp_city", "Напишите город или регион:", gid=g.id)
        g.city = "" if value == "all" else geo.MENU_CITIES[int(value)]
        await submit(cb, s, g)


async def submit(target, s, g: Group) -> None:
    await catalog.finish_submission(s, g)
    await reply(target, "✅ Отправлено на проверку. Как только администратор одобрит — группа попадёт в каталог, "
                "а заявки из неё начнут приносить вам бонусы 🎁",
                kb([[("📂 Мои группы", "cg:my")]]))
    await notify_admins(g)


async def notify_admins(g: Group) -> None:
    from app.bot.handlers.submissions import card

    text, markup = await card(g)
    await notify._send("📝 Новая группа от пользователя:\n\n" + text, markup)


@router.callback_query(F.data == "cg:my")
async def my_groups(cb: CallbackQuery, user: User) -> None:
    async with Session() as s:
        groups = (await s.scalars(select(Group).where(Group.submitted_by == user.tg_id, Group.status != "draft")
                                  .order_by(Group.submitted_at.desc()).limit(30))).all()
        per = await settings.get_int(s, "bonus_leads")
    if not groups:
        return await reply(cb, "Вы пока не добавляли группы.", kb([[("➕ Добавить", "cg:add")], [("◀️ Назад", "cg:menu")]]))
    lines = [f"{KIND[g.kind]} <b>{esc(g.title[:40])}</b> — {STATUS.get(g.status, g.status)}"
             + (f", заявок: {g.leads_count}" if g.catalog else "") for g in groups]
    days = sum(catalog.bonus_days(g, per) for g in groups)
    ad_rows = [[(f"📢 Продвинуть мою группу: {g.title[:20]}", f"ad:ask:{g.id}")] for g in groups if g.catalog]
    await reply(cb, "📂 <b>Мои группы</b>\n\n" + "\n".join(lines) + f"\n\n🎁 Получено бонусных дней: {days}\n🏆 Уровень: {levels.name(user.level)}",
                kb(ad_rows + [[("◀️ Назад", "cg:menu")]]))


@router.callback_query(F.data.regexp(r"^ad:ask:\d+$"))
async def ad_ask(cb: CallbackQuery, user: User) -> None:
    async with Session() as s:
        g = await s.get(Group, int(cb.data.split(":")[2]))
        cost, days = await settings.get_int(s, "ad_leads"), await settings.get_int(s, "ad_days")
    if g is None or g.submitted_by != user.tg_id:
        return await cb.answer("Группа не найдена", show_alert=True)
    until = f"\nСейчас реклама идёт до {g.ad_until:%d.%m.%Y}." if ads.active(g) else ""
    rows = [[("✅ Заказать", f"ad:go:{g.id}")]] if cost > 0 else []
    await reply(cb, (f"📢 <b>Продвижение вашей группы «{esc(g.title[:40])}»</b> в каталоге\n"
                     "<i>Хотите рекламу своего бизнеса или услуги в других группах? Это в 🌐 Каталог → «📣 Реклама моего бизнеса».</i>\n\n"
                     f"Ваша группа будет в начале каталога с пометкой 📢 и в канале статистики на {days} дн. "
                     f"Стоит {cost} заявок из этой группы (доступно: {ads.available(g)}). Дни 💎 за заявки не тратятся.{until}\n"
                     "Рекламу подтверждает администратор." if cost > 0 else "Реклама сейчас не подключена."),
                kb(rows + [[("◀️ Назад", "cg:my")]]))


@router.callback_query(F.data.regexp(r"^ad:go:\d+$"))
async def ad_go(cb: CallbackQuery, user: User) -> None:
    from app.bot.handlers.submissions import ad_card

    async with Session() as s:
        try:
            g = await ads.request(s, user.tg_id, int(cb.data.split(":")[2]))
        except ads.AdError as e:
            return await cb.answer(str(e), show_alert=True)
    text, markup = await ad_card(g, user)
    await notify._send("📢 Заказ рекламы:\n\n" + text, markup)
    await cb.answer("Заявка отправлена администратору", show_alert=True)


@router.callback_query(F.data.regexp(r"^cg:top:(mine|all)$"))
async def top(cb: CallbackQuery, user: User) -> None:
    everything = cb.data.endswith("all") or not user.city
    async with Session() as s:
        board = await levels.rating(s, "" if everything else user.city, limit=0)
    place = next((i for i, (u, _) in enumerate(board, 1) if u.tg_id == user.tg_id), None)
    medals = ["🥇", "🥈", "🥉"]
    lines = [f"{medals[i - 1] if i <= 3 else str(i) + '.'} {esc((u.name or 'Аноним').split()[0][:20])} "
             f"{levels.name(levels.level_of(1, pts))} — {pts} заяв."
             f"{' ← вы' if u.tg_id == user.tg_id else ''}" for i, (u, pts) in enumerate(board[:10], 1)]
    scope = "по всей России" if everything else f"· {esc(user.city)}"
    text = f"🏆 <b>Рейтинг авторов групп</b> {scope}\n\n" + ("\n".join(lines) or "Пока никого — станьте первым!")
    if place and place > 10:
        text += f"\n…\nВаше место: {place}"
    toggle = ("🌍 Вся Россия", "cg:top:all") if not everything else (
        ("📍 Мой город", "cg:top:mine") if user.city else ("◀️ Назад", "cg:menu"))
    await reply(cb, text, kb([[toggle, ("◀️ Каталог", "cg:menu")]]))
