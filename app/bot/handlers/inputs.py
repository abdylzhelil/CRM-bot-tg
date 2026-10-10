"""Обработка текстового ввода после ask(): action из FSM-data определяет, что делать."""
from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from sqlalchemy import delete, select

from app.bot.common import Ask, esc, split_words
from app.bot.handlers import clients as clients_h
from app.db import Session
from app.models import BlacklistEntry, Category, Client, ClientEvent, Keyword, ScoutAccount, SearchQuery
from app.scout.client import parse_proxy
from app.services import rules, settings

router = Router()


@router.message(Ask.value, F.text)
async def on_input(m: Message, state: FSMContext) -> None:
    data = await state.get_data()
    action, text = data["action"], m.text.strip()
    async with Session() as s:
        ok = await HANDLERS[action](s, m, data, text)
    if ok is not False:
        await state.clear()


async def setting(s, m, d, text):
    key = d["key"]
    if key == "mode" and text not in ("semi", "auto"):
        return await m.answer("Нужно semi или auto") and False
    if key != "mode" and not text.isdigit():
        return await m.answer("Нужно число") and False
    await settings.put(s, key, text)
    await m.answer(f"✔ {settings.LABELS[key]}: {esc(text)}")


async def client_field(s, m, d, text, field):
    c = await s.get(Client, d["id"])
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


HANDLERS = {
    "setting": setting,
    "client_find": client_find,
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
