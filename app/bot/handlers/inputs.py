"""Обработка текстового ввода после ask(): action из FSM-data определяет, что делать."""
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from sqlalchemy import select

from app.bot.common import Ask, esc, kb, split_words
from app.bot.handlers import adposts as adposts_h
from app.bot.handlers import blacklist as blacklist_h
from app.bot.handlers import clients as clients_h
from app.db import Session
from app.models import BlacklistEntry, Category, Keyword, ScoutAccount, SearchQuery, UserCategory
from app.services import crm, quiet, rules, settings

router = Router()


# действия, которые доступны только администратору
ADMIN_ACTIONS = ("setting", "kw_", "cat_", "bl_", "acc_", "query_", "us_", "bc_", "apa_", "apg_", "fb_", "gr_")


@router.message(Ask.value, F.text, ~F.text.startswith("/"))  # команда (/ads, /terms…) — это не ответ на вопрос
async def on_input(m: Message, state: FSMContext, user=None) -> None:
    data = await state.get_data()
    action, text = data["action"], m.text.strip()
    if action.startswith(ADMIN_ACTIONS) and not (user and user.is_admin):
        await state.clear()
        return
    async with Session() as s:
        ok = await HANDLERS[action](s, m, data, text)
    if ok is not False:
        await state.clear()


@router.message(Ask.value, ~F.text)
async def on_media(m: Message, state: FSMContext, user=None) -> None:
    """Не текст во время вопроса. Картинка с подписью годится для рекламы; остальное — просим текстом (не молчим)."""
    data = await state.get_data()
    if data.get("action") == "ap_text" and m.photo:
        if m.media_group_id and not m.caption:
            return  # остальные фото альбома: берём только первое (с подписью)
        name = adposts_h.adposts.new_photo_name()
        await m.bot.download(m.photo[-1], destination=adposts_h.adposts.media_path(name))
        async with Session() as s:
            ok = await adposts_h.ap_text(s, m, data, (m.caption or "").strip(), photo=name)
        if ok is False:
            adposts_h.adposts.remove_photo(name)
        else:
            await state.clear()
        return
    hint = " Можно картинку с подписью." if data.get("action") == "ap_text" else ""
    await m.answer("✍️ Пришлите ответ текстом." + hint + "\n/cancel — отмена")


async def setting(s, m, d, text):
    key = d["key"]
    try:
        text = settings.validate(key, text)
    except settings.SettingError as e:
        return await m.answer(str(e)) and False
    await settings.put(s, key, text)
    await m.answer(f"✔ {settings.LABELS[key]}: {esc(text) or 'выключен'}", reply_markup=kb([[("⚙️ К настройкам", "st:menu")]]))


async def setting_find(s, m, d, text):
    keys = settings.search(text)
    if not keys:
        return await m.answer("Ничего не нашлось. Попробуйте другое слово или откройте раздел в ⚙️ Настройках.",
                              reply_markup=kb([[("⚙️ К настройкам", "st:menu")]]))
    rows = [[(settings.SHORT.get(k, settings.LABELS[k]), f"st:{k}")] for k in keys]
    rows.append([("⚙️ К настройкам", "st:menu")])
    await m.answer(f"🔎 Нашлось: {len(keys)}", reply_markup=kb(rows))


async def us_forget(s, m, d, text):
    from app.bot.handlers.users_admin import forget_preview

    await forget_preview(m, text)


async def client_field(s, m, d, text, field):
    from app.bot.handlers.clients import owned

    c = await owned(s, d["id"], m.from_user.id)
    if c is None:
        return await m.answer("Клиент не найден")
    try:
        await crm.update_field(s, c, field, text)
    except crm.CrmError as e:
        return await m.answer("Нужно число" if field == "budget" else str(e)) and False
    await clients_h.card(m, c.id)


async def client_add(s, m, d, text):
    """«Имя @username +79001234567» — части в любом порядке, любые можно опустить."""
    name, username, phone = [], "", ""
    for part in text.split():
        if part.startswith("@"):
            username = part.lstrip("@")
        elif sum(ch.isdigit() for ch in part) >= 7:
            phone = part
        else:
            name.append(part)
    try:
        c = await crm.create_client(s, m.from_user.id, " ".join(name), username, phone)
    except crm.CrmError as e:
        return await m.answer(str(e)) and False
    await clients_h.card(m, c.id)


async def client_find(s, m, d, text):
    await clients_h.show_list(m, "all", text)


async def cat_field(s, m, d, text, field):
    if field == "add":
        parent = d["id"] or None
        s.add(Category(name=text, parent_id=parent))
    else:
        c = await s.get(Category, d["id"])
        if field == "ren":
            c.name = text
        elif field == "tpl":
            c.reply_template = text
        elif field == "prices":
            c.prices = text
        elif field == "city":
            c.city = "" if text == "-" else text
    await s.commit()
    rules.invalidate()
    await m.answer("✔ Готово. Откройте «🗂 Категории», чтобы посмотреть.")


async def kw_add(s, m, d, text):
    cid = d["cid"] or None
    have = {w.lower() for w in (await s.scalars(
        select(Keyword.word).where(Keyword.kind == d["kind"], Keyword.category_id == cid))).all()}
    new = [w for w in split_words(text) if w.lower() not in have]
    s.add_all(Keyword(kind=d["kind"], word=w, category_id=cid) for w in new)
    await s.commit()
    rules.invalidate()
    await m.answer(f"✔ Добавлено: {len(new)}")


async def kw_del(s, m, d, text):
    cid = d["cid"] or None
    low = [w.lower() for w in split_words(text)]
    rows = (await s.scalars(select(Keyword).where(Keyword.kind == d["kind"], Keyword.category_id == cid))).all()
    gone = [k for k in rows if k.word.lower() in low]
    for k in gone:
        await s.delete(k)
    await s.commit()
    rules.invalidate()
    await m.answer(f"✔ Удалено: {len(gone)}")


async def query_add(s, m, d, text):
    have = set((await s.scalars(select(SearchQuery.text))).all())
    new = [w for w in split_words(text) if w not in have]
    s.add_all(SearchQuery(text=w) for w in new)
    await s.commit()
    await m.answer(f"✔ Добавлено запросов: {len(new)}")


async def bl_add(s, m, d, text):
    if not (text.lstrip("-").isascii() and text.lstrip("-").isdigit()) or len(text) > 19:
        return await m.answer("Нужен числовой ID") and False
    uid = int(text)
    if not await s.get(BlacklistEntry, uid):
        s.add(BlacklistEntry(user_id=uid, status="spam", reason="добавлено вручную"))
        await s.commit()
    await m.answer("✔ В чёрном списке")


async def acc_field(s, m, d, text, field):
    a = await s.get(ScoutAccount, d["id"])
    if not text.isdigit():
        return await m.answer("Нужно число") and False
    a.daily_join_limit = int(text)
    await s.commit()
    await m.answer("✔ Сохранено.")


async def neg_add(s, m, d, text):
    from app.bot.handlers.profile import save_negatives

    n = await save_negatives(s, m.from_user.id, split_words(text), remove=False)
    await m.answer(f"✔ Добавлено минус-слов: {n}")


async def neg_del(s, m, d, text):
    from app.bot.handlers.profile import save_negatives

    n = await save_negatives(s, m.from_user.id, split_words(text), remove=True)
    await m.answer(f"✔ Удалено: {n}")


async def my_field(s, m, d, text, field):
    from app.models import User

    u = await s.get(User, m.from_user.id)
    if field == "quiet_hours":
        if text in ("-", "0", "выкл"):
            text = ""
        elif not quiet.parse_hours(text):
            return await m.answer("Формат: 23-8 (с 23:00 до 08:00) или «-», чтобы выключить") and False
    elif text == "-":
        text = ""  # «-» — очистить шаблон/цены
    setattr(u, field, text)
    await s.commit()
    await m.answer("✔ Сохранено. Посмотреть: 👤 Профиль")


async def city_other(s, m, d, text):
    from app.bot.handlers.profile import set_city
    from app.models import User

    import difflib

    from app.bot.common import esc, kb
    from app.bot.handlers import profile as profile_h
    from app.services import geo

    u = await s.get(User, m.from_user.id)
    raw = text.strip()[:64]
    city, region = geo.find(raw)
    if city or region:  # «мхк», «в Махачкале», «Дагестан» → как в меню
        return await set_city(m, u, city or region)
    # незнакомый город или опечатка: молча сохранить — значит почти не получать заявок
    known = geo.KNOWN_PLACES
    close = difflib.get_close_matches(raw.lower(), [k.lower() for k in known], n=3, cutoff=0.7)
    idx = [next(i for i, k in enumerate(known) if k.lower() == c) for c in close]
    profile_h.PENDING_CITY[m.from_user.id] = raw
    rows = [[(f"📍 {known[i]}", f"ob:cityx:{i}")] for i in idx]
    rows += [[("🇷🇺 Вся Россия", "ob:city:all")], [(f"Оставить «{raw[:30]}»", "ob:citykeep")], [("✍️ Написать иначе", "ob:city:other")]]
    await m.answer((f"🤔 Может, вы имели в виду {', '.join(known[i] for i in idx)}?\n\n" if idx else "")
                   + f"Город «{esc(raw)}» бот пока не знает: заявки привязываются к знакомым городам, поэтому с ним "
                   "будут приходить только заявки без указанного места. Выберите вариант:", reply_markup=kb(rows))


async def us_find(s, m, d, text):
    from sqlalchemy import select as _select

    from app.bot.handlers.users_admin import user_card
    from app.models import User

    raw = text.strip().lstrip("@")
    u = await s.get(User, int(raw)) if raw.lstrip("-").isdigit() else await s.scalar(
        _select(User).where(User.username.ilike(raw)))
    if u is None:
        return await m.answer("Не найден. Пользователь должен хотя бы раз написать боту.")
    await user_card(m, u.tg_id)


async def grp_link(s, m, d, text):
    from app.bot.handlers import catalog

    return await catalog.grp_link(s, m, d, text)


async def grp_city(s, m, d, text):
    from app.bot.handlers import catalog

    return await catalog.grp_city(s, m, d, text)


async def pass_to(s, m, d, text):
    from app.bot.handlers import leads

    return await leads.pass_to(s, m, d, text)


async def niche_propose(s, m, d, text):
    from app.bot.handlers.profile import PENDING_PROPOSALS
    from app.models import User
    from app.services import niches

    name, words = niches.parse(text)
    if len(name) >= 3 and len(words) >= niches.MIN_WORDS:
        same = await niches.similar(s, text)
        if same and not any(c.name.casefold() == name.casefold() for c in same):  # точное совпадение — скажет propose
            user = await s.get(User, m.from_user.id)
            mine = {c.id for c in same if await s.get(UserCategory, (user.tg_id, c.id))}
            PENDING_PROPOSALS[user.tg_id] = text
            rows = [[(("✅ " if c.id in mine else "⬜ ") + f"{c.emoji} {c.name}", f"ob:fcat:{c.id}")] for c in same]
            rows.append([("➕ Моей нет — всё равно предложить", "ob:propforce")])
            return await m.answer("🔎 Похоже, такая ниша уже есть — нажмите, чтобы отметить:", reply_markup=kb(rows))
    return await send_proposal(s, m, m.from_user.id, text)


async def send_proposal(s, m, user_id: int, text: str):
    from app.bot import notify
    from app.bot.handlers.categories import proposal_card
    from app.models import User
    from app.services import niches

    user = await s.get(User, user_id)
    try:
        cat = await niches.propose(s, user, text)
    except niches.NicheError as e:
        return await m.answer(f"⚠️ {esc(str(e))}") and False
    if user.is_admin:
        return await m.answer(f"✔ Ниша «{esc(cat.name)}» добавлена и подключена вам. "
                              "Раздел и слова можно поменять: 🛠 Управление → 🗂 Категории.")
    await m.answer("✅ Отправлено администратору. Как только ниша появится в списке — напишу.")
    body, markup = await proposal_card(s, cat, user)
    await notify._send("📌 Новая ниша от пользователя:\n\n" + body, markup)


async def niche_find(s, m, d, text):
    from app.bot.handlers.profile import show_found
    from app.models import User

    await show_found(m, await s.get(User, m.from_user.id), text[:60])


async def bc_text(s, m, d, text):
    from app.bot.handlers import broadcast

    # html_text: жирный, ссылки и т. п. из редактора Telegram сохраняются, а «<50%» не ломает разметку
    return await broadcast.bc_text(s, m, d, (m.html_text or text).strip())


async def my_search(s, m, d, text):
    from app.bot.handlers import mysearch as mysearch_h

    return await mysearch_h.on_input(s, m, d, text)


HANDLERS = {
    "bc_text": bc_text,
    "niche_propose": niche_propose,
    "niche_find": niche_find,
    "pass_to": pass_to,
    "ap_text": adposts_h.ap_text,
    "apa_reason": adposts_h.apa_reason,
    "apa_erid": adposts_h.apa_erid,
    "apa_price": adposts_h.apa_price,
    "apg_price": adposts_h.apg_price,
    "apg_hours": adposts_h.apg_hours,
    "apg_note": adposts_h.apg_note,
    "apg_allprice": adposts_h.apg_allprice,
    "apg_algo": adposts_h.apg_algo,
    "grp_link": grp_link,
    "grp_city": grp_city,
    "us_find": us_find,
    "neg_add": neg_add,
    "neg_del": neg_del,
    "city_other": city_other,
    "my_quiet": lambda s, m, d, t: my_field(s, m, d, t, "quiet_hours"),
    "my_tpl": lambda s, m, d, t: my_field(s, m, d, t, "reply_template"),
    "my_prices": lambda s, m, d, t: my_field(s, m, d, t, "prices"),
    "setting": setting,
    "setting_find": setting_find,
    "us_forget": us_forget,
    "client_find": client_find,
    "client_add": client_add,
    "query_add": query_add,
    "kw_add": kw_add,
    "kw_del": kw_del,
    "bl_add": bl_add,
    "bl_find": lambda s, m, d, t: blacklist_h.bl_find(s, m, d, t),
    "bl_sample": lambda s, m, d, t: blacklist_h.bl_sample(s, m, d, t),
}
for _f in ("note", "budget", "phone", "name"):
    HANDLERS[f"client_{_f}"] = lambda s, m, d, t, _f=_f: client_field(s, m, d, t, _f)
for _f in ("add", "ren", "tpl", "prices", "city"):
    HANDLERS[f"cat_{_f}"] = lambda s, m, d, t, _f=_f: cat_field(s, m, d, t, _f)
HANDLERS["my_search"] = my_search


async def _feedback(s, m, d, text):
    from app.bot.handlers import feedback as fb_h

    return await fb_h.on_input(s, m, d, text)


async def _fb_reply(s, m, d, text):
    from app.bot.handlers import feedback as fb_h

    return await fb_h.on_reply(s, m, d, text)


HANDLERS["feedback"] = _feedback
HANDLERS["fb_reply"] = _fb_reply


async def _cra_codes(s, m, d, text):
    from app.bot.handlers import credits as credits_h

    return await credits_h.on_codes(s, m, d, text)


async def _cra_adj(s, m, d, text):
    from app.bot.handlers import credits as credits_h

    return await credits_h.on_adjust(s, m, d, text)


HANDLERS["cra_codes"] = _cra_codes
HANDLERS["cra_adj"] = _cra_adj


async def _gr_place(s, m, d, text):
    from app.bot.handlers import groups as groups_h

    return await groups_h.on_place(s, m, d, text)


HANDLERS["gr_place"] = _gr_place
HANDLERS["acc_limit"] = lambda s, m, d, t: acc_field(s, m, d, t, "limit")
