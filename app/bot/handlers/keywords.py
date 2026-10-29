from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select

from app.bot.common import ask, esc, kb, reply
from app.db import Session
from app.models import Category, Keyword

router = Router()
KINDS = {
    "intent": "Слова-намерения",
    "negative": "Минус-слова",
    "category": "Слова категории",
    "strong": "Сильные фразы",
}


@router.message(F.text == "🔑 Ключевые слова")
async def keywords_menu(target: Message | CallbackQuery) -> None:
    from app.services import badwords

    async with Session() as s:
        bad = len(await badwords.suspects(s))
        off = len(await badwords.disabled(s))
    await reply(target, "🔑 Общие списки (слова категорий — в разделе «Категории»):", kb([
        [("🎯 Слова-намерения", "kw:view:intent:0")],
        [("⛔ Глобальные минус-слова", "kw:view:negative:0")],
        [(f"🙈 Слова, на которые жмут «Не моё» · {bad}", "kw:bad")],
        [(f"💤 Выключенные слова · {off}", "kw:off")],
    ]))


@router.callback_query(F.data == "kw:bad")
async def bad_words(cb: CallbackQuery) -> None:
    """Слова ниш, по которым приходят «не те» заявки: на них часто жмут «🙈 Не моё» / «🚫 Спам». Выключить — одной кнопкой."""
    from app.services import badwords

    async with Session() as s:
        rows = await badwords.suspects(s)
    if not rows:
        return await reply(cb, "🙈 За 2 недели нет слов, на которые часто жмут «Не моё» ✅\n"
                               "Список наполняется, когда пользователи нажимают «🙈 Не моё» под заявками.",
                           kb([[("◀️ Назад", "kw:menu")]]))
    lines = ["🙈 <b>«Не те» заявки</b> — по этим словам ниши приходят заявки, на которые жмут «🙈 Не моё» или «🚫 Спам» "
             "(за 2 недели). Выключите слово — по нему заявки больше не придут (вернуть: «💤 Выключенные слова»).\n"]
    rows_kb = []
    for i, r in enumerate(rows, 1):
        lines.append(f"{i}. «{esc(r['word'])}» в нише «{esc(r['category'])}» — не моё {r['rejected']} из {r['total']}")
        if r["keyword_id"]:
            rows_kb.append([(f"💤 {i}. «{r['word']}» ({r['category']})"[:60], f"kw:tg:{r['keyword_id']}")])
    rows_kb.append([("◀️ Назад", "kw:menu")])
    await reply(cb, "\n".join(lines), kb(rows_kb))


@router.callback_query(F.data == "kw:off")
async def off_words(cb: CallbackQuery) -> None:
    from app.services import badwords

    async with Session() as s:
        rows = await badwords.disabled(s)
        cats = {c.id: c.name for c in (await s.scalars(select(Category))).all()}
    if not rows:
        return await reply(cb, "💤 Выключенных слов нет.", kb([[("◀️ Назад", "kw:menu")]]))
    rows_kb = [[(f"↩️ «{k.word}» ({cats.get(k.category_id, '—')})"[:60], f"kw:tg:{k.id}")] for k in rows[:30]]
    rows_kb.append([("◀️ Назад", "kw:menu")])
    await reply(cb, "💤 <b>Выключенные слова</b> — заявки по ним не приходят. Нажмите, чтобы включить обратно.", kb(rows_kb))


@router.callback_query(F.data.regexp(r"^kw:tg:\d+$"))
async def toggle_word(cb: CallbackQuery) -> None:
    from app.services import badwords

    async with Session() as s:
        kw = await badwords.toggle(s, int(cb.data.split(":")[2]))
        await s.commit()
    if kw is None:
        return await cb.answer("Слово не найдено", show_alert=True)
    off = kw.kind.startswith("x_")
    await cb.answer(f"«{kw.word}» " + ("выключено — заявки по нему больше не придут" if off else "снова включено"),
                    show_alert=True)
    await (bad_words(cb) if off else off_words(cb))


@router.callback_query(F.data.startswith("kw:view:"))
async def view(cb: CallbackQuery) -> None:
    _, _, kind, cid = cb.data.split(":")
    cid = int(cid)
    async with Session() as s:
        words = (await s.scalars(select(Keyword.word).where(
            Keyword.kind == kind, Keyword.category_id == (cid or None)).order_by(Keyword.id))).all()
        cat = await s.get(Category, cid) if cid else None
    title = KINDS[kind] + (f" · {cat.name}" if cat else "")
    back = f"cat:open:{cid}" if cid else "kw:menu"
    await reply(cb, f"<b>{esc(title)}</b>\n" + (esc(", ".join(words)) or "— пусто —"), kb([
        [("➕ Добавить", f"kw:add:{kind}:{cid}"), ("➖ Удалить", f"kw:del:{kind}:{cid}")],
        [("◀️ Назад", back)],
    ]))


@router.callback_query(F.data == "kw:menu")
async def back(cb: CallbackQuery) -> None:
    await keywords_menu(cb)


@router.callback_query(F.data.regexp(r"^kw:(add|del):\w+:\d+$"))
async def edit(cb: CallbackQuery, state: FSMContext) -> None:
    _, op, kind, cid = cb.data.split(":")
    verb = "Добавить" if op == "add" else "Удалить"
    await ask(cb, state, f"kw_{op}", f"{verb} слова/фразы (через запятую или с новой строки). "
              "Склонения учитываются автоматически.", kind=kind, cid=int(cid))
