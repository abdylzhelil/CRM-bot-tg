"""Регистрация и профиль пользователя: ниши, город, минус-слова, тишина, шаблон, тариф."""
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from sqlalchemy import delete, func, select

from app.bot.common import ask, esc, kb, reply
from app.db import Session
from app.models import PLANS, Category, Delivery, Lead, User, UserCategory, UserNegative
from app.services import autosetup, geo, payments, referrals, settings
from app.services.distribute import is_paid, preview

router = Router()

WELCOME = (
    "📡 <b>Привет! Я КлиентРадар — нахожу клиентов в Telegram-группах</b> и помогаю довести их до сделки.\n\n"
    "Настроим за 30 секунд:\n1️⃣ выберите свои ниши\n2️⃣ выберите город\n— и сразу покажу заявки за последние сутки."
)


async def niches_keyboard(user_id: int, onboarding: bool):
    async with Session() as s:
        roots = (await s.scalars(
            select(Category).where(Category.parent_id.is_(None), Category.enabled).order_by(Category.id)
        )).all()
        mine = set((await s.scalars(select(UserCategory.category_id).where(UserCategory.user_id == user_id))).all())
    rows = [[(("✅ " if c.id in mine else "⬜ ") + f"{c.emoji} {c.name}", f"ob:cat:{c.id}")] for c in roots]
    rows.append([("➕ Предложить свою нишу", "ob:propose")])
    rows.append([("Далее ➡️" if onboarding else "✔ Готово", "ob:done")])
    return kb(rows), len(mine)


async def show_niches(target, user: User) -> None:
    markup, count = await niches_keyboard(user.tg_id, not user.onboarded)
    text = "🎯 <b>Ваши ниши</b> — по ним приходят заявки. Отметьте одну или несколько:"
    if not user.onboarded:
        async with Session() as s:
            days = await settings.get_int(s, "cleanup_days")
        if days > 0:
            text += f"\n\n<i>ℹ️ Если не закончить настройку за {days} дн., аккаунт удалится (можно зарегистрироваться заново).</i>"
    if not user.onboarded and isinstance(target, Message):
        text = WELCOME + "\n\n" + text
    await reply(target, text, markup)


def city_keyboard(user: User):
    rows = [[(("📍 " if user.city == c else "") + c, f"ob:city:{i}")] for i, c in enumerate(geo.MENU_CITIES)]
    rows.append([(("📍 " if not user.city else "") + "🇷🇺 Вся Россия", "ob:city:all"), ("✍️ Другой город", "ob:city:other")])
    if user.onboarded and user.city:
        rows.append([(("✅" if user.only_city else "⬜") + " Только мой город (без заявок без адреса)", "ob:only")])
    return kb(rows)


async def show_city(target, user: User) -> None:
    current = user.city or "вся Россия"
    await reply(target, f"📍 <b>Город</b>: сейчас — {esc(current)}.\nЗаявки придут по этому городу и по его региону.",
                city_keyboard(user))


@router.message(F.text == "🎯 Мои ниши")
async def niches_menu(m: Message, user: User) -> None:
    await show_niches(m, user)


@router.message(F.text == "📍 Мой город")
async def city_menu(m: Message, user: User) -> None:
    await show_city(m, user)


@router.callback_query(F.data.startswith("ob:cat:"))
async def toggle_niche(cb: CallbackQuery, user: User) -> None:
    cid = int(cb.data.split(":")[2])
    async with Session() as s:
        row = await s.get(UserCategory, (user.tg_id, cid))
        if row:
            await s.delete(row)
        elif await s.get(Category, cid):
            s.add(UserCategory(user_id=user.tg_id, category_id=cid))
        await s.commit()
    await show_niches(cb, user)


@router.callback_query(F.data == "reg:go")
async def resume_registration(cb: CallbackQuery, user: User) -> None:
    """Кнопка из предупреждения об удалении: продолжить настройку."""
    await cb.answer()
    if user.onboarded:
        return await cb.message.answer("Настройка уже завершена 👍")
    await show_niches(cb.message, user)


@router.callback_query(F.data == "ob:propose")
async def propose_niche(cb: CallbackQuery, state: FSMContext) -> None:
    await ask(cb, state, "niche_propose", (
        "➕ <b>Своя ниша</b>\nНапишите название и через двоеточие слова, по которым искать заявки (минимум 3):\n\n"
        "<code>Ремонт квартир: ремонт, отделка, плитка, натяжные потолки</code>\n\n"
        "Администратор проверит, и ниша появится в списке (вам подключится сразу)."))


@router.callback_query(F.data == "ob:done")
async def niches_done(cb: CallbackQuery, user: User) -> None:
    async with Session() as s:
        count = await s.scalar(select(func.count()).select_from(UserCategory).where(UserCategory.user_id == user.tg_id))
    if not count:
        return await cb.answer("Выберите хотя бы одну нишу", show_alert=True)
    if not user.onboarded:
        return await show_city(cb, user)
    await cb.answer("Сохранено")
    await profile(cb, user)


async def set_city(target, user: User, city: str) -> None:
    async with Session() as s:
        row = await s.get(User, user.tg_id)
        row.city = city
        first = not row.onboarded
        row.onboarded = True
        await s.commit()
    user.city, user.onboarded = city, True
    if first:
        await finish_onboarding(target, user)
    else:
        await show_city(target, user)


@router.callback_query(F.data.startswith("ob:city:"))
async def choose_city(cb: CallbackQuery, user: User, state: FSMContext) -> None:
    value = cb.data.split(":")[2]
    if value == "other":
        return await ask(cb, state, "city_other", "Напишите ваш город (например, Избербаш или Краснодар):")
    city = "" if value == "all" else geo.MENU_CITIES[int(value)]
    await set_city(cb, user, city)


@router.callback_query(F.data == "ob:only")
async def toggle_only_city(cb: CallbackQuery, user: User) -> None:
    async with Session() as s:
        row = await s.get(User, user.tg_id)
        row.only_city = not row.only_city
        user.only_city = row.only_city
        await s.commit()
    await show_city(cb, user)


async def finish_onboarding(target, user: User) -> None:
    from app.bot.handlers.leads import send_card
    from app.bot.setup import menu_keyboard

    msg = target.message if isinstance(target, CallbackQuery) else target
    if isinstance(target, CallbackQuery):
        await target.answer()
    async with Session() as s:
        niches = (await s.scalars(select(Category.name).join(UserCategory, UserCategory.category_id == Category.id)
                                  .where(UserCategory.user_id == user.tg_id))).all()
        shown = await preview(s, user)
    async with Session() as s:
        referrer_id = await referrals.reward_signup(s, user)
        days = await settings.get_int(s, "ref_days")
    if referrer_id:
        from app.bot import notify

        await msg.answer(f"🎁 Вы пришли по приглашению — дарим +{days} дн. 💎 Полного тарифа!")
        try:
            await notify._bot.send_message(referrer_id, f"🤝 По вашей ссылке пришёл новый пользователь — вам +{days} дн. 💎!")
        except Exception:
            pass
    if not user.is_admin:
        from app.bot import notify

        who = esc(user.name or str(user.tg_id)) + (f" (@{esc(user.username)})" if user.username else "")
        await notify._send(f"🆕 Новый пользователь: {who}\nНиши: {esc(', '.join(niches))}\n"
                           f"Город: {esc(user.city or 'вся Россия')}")
    async with Session() as s:
        await msg.answer(
            "✅ Готово! Новые заявки по вашим нишам будут приходить сюда.\n"
            + (f"Вот что было за последние сутки ({len(shown)}):" if shown else
               "За последние сутки подходящих заявок не было — пришлю, как только появятся."),
            reply_markup=menu_keyboard(user),
        )
        for d in shown:
            await send_card(msg, s, d, await s.get(Lead, d.lead_id), user)
    await msg.answer(
        "💡 <b>Как работать с заявкой</b>\n"
        "1️⃣ <b>✍️ Ответить</b> — откроется чат с человеком; пишите быстро, заявка живёт минуты.\n"
        "2️⃣ <b>📋 Шаблон</b> — готовый текст ответа с вашими ценами (свои цены и шаблон: 👤 Профиль).\n"
        "3️⃣ <b>✅ В работу</b> — человек попадёт в 👥 Клиенты: этапы сделки, заметки, напоминания.\n"
        "4️⃣ Не ваше? <b>❌ Отказ</b> или <b>🚫 Спам</b> — похожие больше не придут.\n\n"
        "Больше заявок: 🌐 Каталог (добавьте свою группу — получите дни 💎), 🤝 приглашения друзей. Подробнее — /help.")


# ---------- профиль ----------
@router.message(F.text == "👤 Профиль")
async def profile_menu(m: Message, user: User) -> None:
    await profile(m, user)


async def plan_line(s, user: User) -> str:
    if is_paid(user):
        until = f" до {user.plan_until:%d.%m.%Y}" if user.plan_until else " (бессрочно)"
        return f"{PLANS['full']}{until} — заявки сразу и без лимита"
    limit = await settings.get_int(s, "free_daily_limit")
    delay = await settings.get_int(s, "free_delay_min")
    return f"{PLANS['free']} — до {limit} заявок в день, с задержкой {delay} мин"


async def profile(target, user: User) -> None:
    async with Session() as s:
        user = await s.get(User, user.tg_id)
        niches = (await s.scalars(
            select(Category.name).join(UserCategory, UserCategory.category_id == Category.id)
            .where(UserCategory.user_id == user.tg_id)
        )).all()
        negatives = (await s.scalars(select(UserNegative.word).where(UserNegative.user_id == user.tg_id))).all()
        total = await s.scalar(select(func.count()).select_from(Delivery).where(Delivery.user_id == user.tg_id))
        plan = await plan_line(s, user)
    text = (
        f"👤 <b>Профиль</b>\n"
        f"Тариф: {plan}\n"
        f"Ниши: {esc(', '.join(niches)) or '—'}\n"
        f"Город: {esc(user.city or 'вся Россия')}{' (только мой город)' if user.only_city else ''}\n"
        f"Минус-слова: {esc(', '.join(negatives)) or '—'}\n"
        f"Режим тишины: {esc(user.quiet_hours) or 'выключен'}\n"
        f"Получено заявок: {total}\n"
        f"{'⏸ Заявки на паузе' if user.paused else ''}"
    )
    await reply(target, text, kb([
        [("🎯 Ниши", "pf:niches"), ("📍 Город", "pf:city")],
        [("⛔ Минус-слова", "pf:neg"), ("🌙 Тишина", "pf:quiet")],
        [("📋 Мой шаблон ответа", "pf:tpl"), ("💰 Мои цены", "pf:prices")],
        [("▶️ Возобновить" if user.paused else "⏸ Пауза", "pf:pause"), ("💎 Тариф", "pf:plan")],
        [("⚡ Автонастройка", "pf:auto"), ("🤝 Пригласить друзей", "pf:ref")],
    ]))


@router.callback_query(F.data == "pf:auto")
async def pf_auto(cb: CallbackQuery, user: User) -> None:
    """Автонастройка: показываем, что будет заполнено (только пустое) и что надо выбрать самому."""
    async with Session() as s:
        row = await s.get(User, user.tg_id)
        intro = await autosetup.user_intro(s, row)
        plan = await autosetup.user_plan(s, row)
        missing = await autosetup.user_missing(s, row)
    text = f"⚡ <b>Автонастройка</b>\n{intro}\n\n"
    if plan:
        text += "Заполню то, что у вас пусто:\n" + "\n".join(f"• {esc(k)}: {esc(v)}" for k, v in plan)
    else:
        text += "Всё основное уже настроено 👍"
    if missing:
        text += "\n\nОсталось сделать самому:\n" + "\n".join(f"• {esc(m)}" for m in missing)
    rows = [[("✅ Применить", "pf:autook")]] if plan else []
    if user.is_admin:
        rows.append([("🛠 Автонастройка бота", "st:auto")])
    rows.append([("◀️ Профиль", "pf:back")])
    await reply(cb, text, kb(rows))


@router.callback_query(F.data == "pf:autook")
async def pf_auto_apply(cb: CallbackQuery, user: User) -> None:
    async with Session() as s:
        n = await autosetup.user_apply(s, user)
    await cb.answer(f"Готово, настроено пунктов: {n}", show_alert=True)
    await profile(cb, user)


@router.callback_query(F.data == "pf:niches")
async def pf_niches(cb: CallbackQuery, user: User) -> None:
    await show_niches(cb, user)


@router.callback_query(F.data == "pf:city")
async def pf_city(cb: CallbackQuery, user: User) -> None:
    await show_city(cb, user)


@router.callback_query(F.data == "pf:pause")
async def pf_pause(cb: CallbackQuery, user: User) -> None:
    async with Session() as s:
        row = await s.get(User, user.tg_id)
        row.paused = not row.paused
        await s.commit()
    await cb.answer("Заявки на паузе" if row.paused else "Заявки снова приходят")
    await profile(cb, row)


@router.callback_query(F.data == "pf:neg")
async def pf_neg(cb: CallbackQuery, user: User) -> None:
    await reply(cb, (
        "⛔ <b>Минус-слова</b>: заявки с ними вам не придут (например, «бесплатно», «вакансия», «Москва»).\n"
        "Склонения учитываются автоматически."
    ), kb([[("➕ Добавить", "pf:neg_add"), ("➖ Удалить", "pf:neg_del")], [("◀️ Профиль", "pf:back")]]))


@router.callback_query(F.data.in_({"pf:neg_add", "pf:neg_del", "pf:quiet", "pf:tpl", "pf:prices"}))
async def pf_ask(cb: CallbackQuery, state: FSMContext) -> None:
    prompts = {
        "pf:neg_add": ("neg_add", "Минус-слова через запятую:"),
        "pf:neg_del": ("neg_del", "Какие минус-слова удалить (через запятую):"),
        "pf:quiet": ("my_quiet", "Режим тишины: с какого по какой час копить обычные заявки, например 23-8. "
                                 "🔥 Горячие придут сразу. «-» — выключить:"),
        "pf:tpl": ("my_tpl", "Ваш шаблон ответа клиенту. Можно использовать {имя}, {цены}, {группа}, {категория}.\n"
                             "Например: Здравствуйте{, имя}! Увидел ваш запрос в «{группа}». {цены} Когда удобно обсудить?"),
        "pf:prices": ("my_prices", "Ваши цены одной строкой (например: лендинг от 15 000 ₽, магазин от 40 000 ₽):"),
    }
    action, prompt = prompts[cb.data]
    await ask(cb, state, action, prompt)


@router.callback_query(F.data == "pf:back")
async def pf_back(cb: CallbackQuery, user: User) -> None:
    await profile(cb, user)


async def _invite(target, user: User, back: bool) -> None:
    from urllib.parse import quote

    bot = target.bot
    async with Session() as s:
        days = await settings.get_int(s, "ref_days")
        percent = await settings.get_int(s, "ref_percent")
        total, done = await referrals.stats(s, user.tg_id)
    link = referrals.link((await bot.me()).username, user.tg_id)
    share = f"https://t.me/share/url?url={quote(link, safe='')}&text={quote('КлиентРадар — заявки от клиентов из Telegram-групп. Заходи:')}"
    rows = [[("📤 Отправить друзьям", f"url:{share}")]]
    if back:
        rows.append([("◀️ Профиль", "pf:back")])
    await reply(target, (
        "🤝 <b>Пригласите друзей</b>\n\n"
        f"Друг заходит по вашей ссылке и настраивает бота — вы оба получаете <b>+{days} дней 💎</b>. "
        f"Дальше, когда он зарабатывает бонусные дни за свои группы, вам идёт ещё {percent}% от них.\n\n"
        "Нажмите «📤 Отправить друзьям» — выберите чат, и ссылка уйдёт сама.\n"
        f"Или скопируйте (нажмите на ссылку):\n<code>{link}</code>\n\n"
        f"Приглашено: {total}, завершили настройку: {done}"
    ), kb(rows))


@router.callback_query(F.data == "pf:ref")
async def pf_ref(cb: CallbackQuery, user: User) -> None:
    await _invite(cb, user, True)


@router.message(Command("invite"))
async def invite_cmd(msg: Message, user: User) -> None:
    await _invite(msg, user, False)


@router.callback_query(F.data == "pf:plan")
async def pf_plan(cb: CallbackQuery, user: User) -> None:
    async with Session() as s:
        line = await plan_line(s, user)
        admin = await s.scalar(select(User).where(User.is_admin, User.username != "").limit(1))
        offers = await payments.packages(s)  # цены видны и до подключения оплаты: их проверяет ЮKassa по скриншотам
    contact = f"@{admin.username}" if admin else "администратору бота"
    how = ("Выберите пакет — оплата прямо в Telegram, тариф продлится сразу." if offers
           else f"Подключить: напишите {contact}.")
    if offers and payments.enabled():
        how += ("\n\n💡 Если окно оплаты не открывается или долго грузится (бывает при включённом VPN) — "
                "на минуту отключите VPN или выберите сервер в России и нажмите кнопку ещё раз. "
                f"Не получается — напишите {contact}, подключим вручную.")
    if offers and not payments.enabled():
        how += f"\n⏳ Онлайн-оплату подключаем. Пока можно оплатить, написав {contact}."
    if user.renew_method:
        how += f"\n\n🔁 <b>Автопродление включено</b> ({user.renew_days} дн.). Отключить можно кнопкой ниже."
    rows = [[(f"💳 {d} дн. — {price} ₽", f"pay:{d}")] for d, price in offers]
    if offers and payments.links_enabled():
        rows.append([("📱 Оплатить через СБП / по ссылке", "lnk:menu")])
    if user.renew_method:
        rows.append([("⛔ Отключить автопродление", "ren:off")])
    await reply(cb, (
        f"💎 <b>Тариф</b>\nСейчас: {line}\n\n"
        f"{PLANS['full']}: заявки приходят сразу (а заявка живёт минуты!), без дневного лимита, "
        f"вы — среди первых получателей.\n\n{how}\n"
        "Бонусные дни можно заработать: 🌐 Каталог и 🤝 Приглашения."
    ), kb(rows + [[("◀️ Профиль", "pf:back")]]))


# ---------- ввод (вызывается из inputs.py) ----------
async def save_negatives(s, user_id: int, words: list[str], remove: bool) -> int:
    have = {w.word.lower(): w for w in (await s.scalars(select(UserNegative).where(UserNegative.user_id == user_id))).all()}
    changed = 0
    for w in words:
        if remove and w.lower() in have:
            await s.delete(have[w.lower()])
            changed += 1
        elif not remove and w.lower() not in have:
            s.add(UserNegative(user_id=user_id, word=w))
            changed += 1
    await s.commit()
    return changed


async def clear_user_data(s, user_id: int) -> None:
    """Для тестов и удаления аккаунта."""
    await s.execute(delete(UserNegative).where(UserNegative.user_id == user_id))
    await s.execute(delete(UserCategory).where(UserCategory.user_id == user_id))
