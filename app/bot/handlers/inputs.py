"""Обработка текстового ввода после ask(): action из FSM-data определяет, что делать."""
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from sqlalchemy import select

from app.bot.common import Ask, esc, kb, split_words
from app.bot.handlers import clients as clients_h
from app.db import Session
from app.models import BlacklistEntry, Category, Client, ClientEvent, Keyword, ScoutAccount, SearchQuery
from app.scout.client import parse_proxy
from app.services import quiet, rules, settings

router = Router()


# действия, которые доступны только администратору
ADMIN_ACTIONS = ("setting", "kw_", "cat_", "bl_", "acc_", "query_", "us_", "bc_")


@router.message(Ask.value, F.text)
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


async def setting(s, m, d, text):
    key = d["key"]
    if key == "mode" and text not in ("semi", "auto"):
        return await m.answer("Нужно semi или auto") and False
    if key == "quiet_hours":
        if text in ("-", "0", "выкл"):
            text = ""
        elif not quiet.parse_hours(text):
            return await m.answer("Формат: 23-8 (с 23:00 до 08:00) или «-», чтобы выключить") and False
    elif key == "report_hour":
        if not text.isdigit() or int(text) > 23:
            return await m.answer("Нужен час от 0 до 23") and False
    elif key == "ai_model":
        from app.services.ai import MODELS

        if text not in MODELS:
            return await m.answer("Допустимо: " + ", ".join(MODELS)) and False
    elif key != "mode" and not text.isdigit():
        return await m.answer("Нужно число") and False
    await settings.put(s, key, text)
    await m.answer(f"✔ {settings.LABELS[key]}: {esc(text) or 'выключен'}", reply_markup=kb([[("⚙️ К настройкам", "st:menu")]]))


async def client_field(s, m, d, text, field):
    from app.bot.handlers.clients import owned

    c = await owned(s, d["id"], m.from_user.id)
    if c is None:
        return await m.answer("Клиент не найден")
    if field == "note":
        c.notes = (c.notes + "\n" if c.notes else "") + text
        s.add(ClientEvent(client_id=c.id, text=f"Заметка: {text[:200]}"))
    elif field == "budget":
        digits = "".join(ch for ch in text if ch.isdigit())
        if not digits:
            return await m.answer("Нужно число") and False
        c.budget = int(digits)
    else:
        c.phone = text
    await s.commit()
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
    c = Client(user_id=m.from_user.id, name=" ".join(name), username=username, phone=phone, source="добавлен вручную")
    s.add(c)
    await s.flush()
    s.add(ClientEvent(client_id=c.id, text="Добавлен вручную"))
    await s.commit()
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
    if not text.lstrip("-").isdigit():
        return await m.answer("Нужен числовой ID") and False
    uid = int(text)
    if not await s.get(BlacklistEntry, uid):
        s.add(BlacklistEntry(user_id=uid, status="spam", reason="добавлено вручную"))
        await s.commit()
    await m.answer("✔ В чёрном списке")


async def acc_field(s, m, d, text, field):
    a = await s.get(ScoutAccount, d["id"])
    if field == "limit":
        if not text.isdigit():
            return await m.answer("Нужно число") and False
        a.daily_join_limit = int(text)
    else:
        proxy = "" if text == "-" else text
        try:
            parse_proxy(proxy)
        except ValueError as e:
            return await m.answer(str(e)) and False
        a.proxy = proxy
    await s.commit()
    await m.answer("✔ Сохранено. Нажмите «🔁 Перезапустить» у аккаунта, чтобы применить прокси.")


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
    setattr(u, field, text)
    await s.commit()
    await m.answer("✔ Сохранено. Посмотреть: 👤 Профиль")


async def city_other(s, m, d, text):
    from app.bot.handlers.profile import set_city
    from app.models import User

    u = await s.get(User, m.from_user.id)
    await set_city(m, u, text.strip()[:64])


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
    from app.bot import notify
    from app.bot.handlers.categories import proposal_card
    from app.models import User
    from app.services import niches

    user = await s.get(User, m.from_user.id)
    try:
        cat = await niches.propose(s, user, text)
    except niches.NicheError as e:
        return await m.answer(f"⚠️ {esc(str(e))}") and False
    if user.is_admin:
        return await m.answer(f"✔ Ниша «{esc(cat.name)}» добавлена и подключена вам. "
                              "Слова можно уточнить: 🛠 Управление → 🗂 Категории.")
    await m.answer("✅ Отправлено администратору. Как только ниша появится в списке — напишу.")
    body, markup = await proposal_card(s, cat, user)
    await notify._send("📌 Новая ниша от пользователя:\n\n" + body, markup)


async def bc_text(s, m, d, text):
    from app.bot.handlers import broadcast

    return await broadcast.bc_text(s, m, d, text)


HANDLERS = {
    "bc_text": bc_text,
    "niche_propose": niche_propose,
    "pass_to": pass_to,
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
    "client_find": client_find,
    "client_add": client_add,
    "query_add": query_add,
    "kw_add": kw_add,
    "kw_del": kw_del,
    "bl_add": bl_add,
}
for _f in ("note", "budget", "phone"):
    HANDLERS[f"client_{_f}"] = lambda s, m, d, t, _f=_f: client_field(s, m, d, t, _f)
for _f in ("add", "ren", "tpl", "prices", "city"):
    HANDLERS[f"cat_{_f}"] = lambda s, m, d, t, _f=_f: cat_field(s, m, d, t, _f)
for _f in ("limit", "proxy"):
    HANDLERS[f"acc_{_f}"] = lambda s, m, d, t, _f=_f: acc_field(s, m, d, t, _f)
