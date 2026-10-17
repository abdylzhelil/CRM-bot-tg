from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import delete, func, select

from app.bot.common import ask, esc, kb, reply
from app.db import Session
from app.models import BlacklistEntry, FilteredMessage, LabeledText, SpamSample, SpamVote
from app.services import spamtexts

router = Router()
PAGE = 10
TITLES = {"spam": "🚫 Чёрный список", "client": "👍 Белый список (это клиенты)"}


def who(e: BlacklistEntry) -> str:
    return f"@{e.username}" if e.username else str(e.user_id)


@router.message(F.text == "🛡 Чёрный список")
async def blacklist_menu(target: Message | CallbackQuery) -> None:
    async with Session() as s:
        cnt = dict((await s.execute(select(BlacklistEntry.status, func.count()).group_by(BlacklistEntry.status))).all())
        samples = await spamtexts.count(s)
        filtered = await s.scalar(select(func.count()).select_from(FilteredMessage)) or 0
        labeled = await s.scalar(select(func.count()).select_from(LabeledText)) or 0
    await reply(target, "🛡 <b>Списки авторов заявок</b>\n"
                "🚫 Чёрный — их сообщения не становятся заявками.\n👍 Белый — всегда заявки, даже если похожи на спам.\n"
                "🧩 Спам-тексты — похожий текст с любого аккаунта не станет заявкой (появляются, когда вы жмёте «🚫 Спам»).\n"
                "🙅 Отсеянные — реклама услуг от исполнителей (не заказчики) за 3 дня; можно вернуть «↩️ Это заявка».",
                kb([[(f"🚫 Чёрный · {cnt.get('spam', 0)}", "bl:ls:spam:0"), (f"👍 Белый · {cnt.get('client', 0)}", "bl:ls:client:0")],
                    [(f"🧩 Спам-тексты · {samples}", "bl:sp:0"), (f"🙅 Отсеянные · {filtered}", "bl:fl:0")],
                    [(f"🎓 Обучение фильтра · {labeled}", "bl:lb:0")],
                    [("🔎 Найти по ID или @нику", "bl:find"), ("➕ Добавить по ID", "bl:add")]]))


@router.callback_query(F.data == "bl:menu")
async def back(cb: CallbackQuery) -> None:
    await blacklist_menu(cb)


@router.callback_query(F.data.regexp(r"^bl:ls:(spam|client):\d{1,6}$"))
async def show_list(cb: CallbackQuery) -> None:
    """Все записи страницами по 10, новые сверху; нажатие — карточка с кнопкой «убрать»."""
    _, _, status, raw = cb.data.split(":")
    offset = int(raw)
    async with Session() as s:
        total = await s.scalar(select(func.count()).select_from(BlacklistEntry).where(BlacklistEntry.status == status)) or 0
        rows_ = (await s.scalars(select(BlacklistEntry).where(BlacklistEntry.status == status)
                                 .order_by(BlacklistEntry.created_at.desc()).offset(offset).limit(PAGE))).all()
    rows = [[(f"{who(e)} · {e.reason[:30]}" if e.reason else who(e), f"bl:o:{e.user_id}")] for e in rows_]
    nav = []
    if offset:
        nav.append(("⬅️", f"bl:ls:{status}:{max(offset - PAGE, 0)}"))
    if offset + PAGE < total:
        nav.append(("➡️", f"bl:ls:{status}:{offset + PAGE}"))
    if nav:
        rows.append(nav)
    rows.append([("◀️ Назад", "bl:menu")])
    page = f" (с {offset + 1} по {offset + len(rows_)} из {total})" if total > PAGE else f": {total}"
    await reply(cb, f"{TITLES[status]}{page}" + ("" if rows_ else "\nПусто."), kb(rows))


async def card(target, user_id: int) -> None:
    async with Session() as s:
        e = await s.get(BlacklistEntry, user_id)
        votes = await s.scalar(select(func.count()).select_from(SpamVote).where(SpamVote.author_id == user_id)) or 0
    if e is None:
        return await reply(target, "Этого автора в списках нет.", kb([[("◀️ Назад", "bl:menu")]]))
    text = (f"{TITLES[e.status] if e.status in TITLES else e.status}\n"
            f"Автор: {esc(who(e))} (ID {e.user_id})\nПричина: {esc(e.reason or '—')}\n"
            f"Добавлен: {e.created_at:%d.%m.%Y}" + (f"\nГолосов «спам»: {votes}" if votes else ""))
    await reply(target, text, kb([[("♻️ Убрать из списка", f"bl:rm:{e.user_id}")],
                                  [("◀️ К списку", f"bl:ls:{e.status}:0")]]))


@router.callback_query(F.data.regexp(r"^bl:o:-?\d{1,19}$"))
async def open_entry(cb: CallbackQuery) -> None:
    await card(cb, int(cb.data.split(":")[2]))


@router.callback_query(F.data.regexp(r"^bl:rm:-?\d{1,19}$"))
async def remove(cb: CallbackQuery) -> None:
    """Убрать из списка. Голоса «спам» тоже снимаем — иначе первый же новый голос вернул бы автора в чёрный список."""
    uid = int(cb.data.split(":")[2])
    async with Session() as s:
        e = await s.get(BlacklistEntry, uid)
        status = e.status if e else "spam"
        if e:
            await s.delete(e)
        await s.execute(delete(SpamVote).where(SpamVote.author_id == uid))
        await s.commit()
    await cb.answer("Убрано из списка")
    cb.data = f"bl:ls:{status}:0"
    await show_list(cb)


@router.callback_query(F.data == "bl:add")
async def add(cb: CallbackQuery, state: FSMContext) -> None:
    await ask(cb, state, "bl_add", "Telegram ID пользователя (числом):")


@router.callback_query(F.data == "bl:find")
async def find(cb: CallbackQuery, state: FSMContext) -> None:
    await ask(cb, state, "bl_find", "ID или @ник автора:")


async def bl_find(s, m: Message, d, text):
    raw = text.strip().lstrip("@")
    if not raw:
        return await m.answer("Напишите ID или @ник") and False
    if raw.lstrip("-").isascii() and raw.lstrip("-").isdigit() and len(raw) < 20:
        e = await s.get(BlacklistEntry, int(raw))
    else:
        e = await s.scalar(select(BlacklistEntry).where(func.lower(BlacklistEntry.username) == raw.lower()).limit(1))
    if e is None:
        return await m.answer("Не нашёл в списках.", reply_markup=kb([[("◀️ Назад", "bl:menu")]]))
    await card(m, e.user_id)


# ---------- спам-тексты ----------
@router.callback_query(F.data.regexp(r"^bl:sp:\d{1,6}$"))
async def samples_list(cb: CallbackQuery) -> None:
    offset = int(cb.data.split(":")[2])
    async with Session() as s:
        total = await spamtexts.count(s)
        rows_ = (await s.scalars(select(SpamSample).order_by(SpamSample.id.desc()).offset(offset).limit(PAGE))).all()
    rows = [[(f"№{x.id} · {' '.join(x.text.split())[:40]}" + (f" · отсеяно {x.hits}" if x.hits else ""), f"bl:so:{x.id}")]
            for x in rows_]
    nav = []
    if offset:
        nav.append(("⬅️", f"bl:sp:{max(offset - PAGE, 0)}"))
    if offset + PAGE < total:
        nav.append(("➡️", f"bl:sp:{offset + PAGE}"))
    if nav:
        rows.append(nav)
    rows.append([("➕ Добавить спам-текст", "bl:spadd")])
    rows.append([("◀️ Назад", "bl:menu")])
    await reply(cb, f"🧩 Спам-тексты: {total}" + ("" if rows_ else "\nПока пусто — появятся, когда вы отметите заявку «🚫 Спам»."),
                kb(rows))


@router.callback_query(F.data.regexp(r"^bl:so:\d{1,9}$"))
async def sample_open(cb: CallbackQuery) -> None:
    async with Session() as s:
        x = await s.get(SpamSample, int(cb.data.split(":")[2]))
    if x is None:
        return await cb.answer("Уже удалён", show_alert=True)
    await reply(cb, f"🧩 Спам-текст №{x.id} (добавлен {x.created_at:%d.%m.%Y}, отсеяно похожих: {x.hits})\n\n{esc(x.text[:1500])}",
                kb([[("♻️ Удалить (это не спам)", f"bl:sdel:{x.id}")], [("◀️ К списку", "bl:sp:0")]]))


@router.callback_query(F.data.regexp(r"^bl:sdel:\d{1,9}$"))
async def sample_delete(cb: CallbackQuery) -> None:
    async with Session() as s:
        x = await s.get(SpamSample, int(cb.data.split(":")[2]))
        if x is not None:
            await s.delete(x)
            await s.commit()
    spamtexts.invalidate()
    await cb.answer("Удалён — похожие тексты снова могут быть заявками")
    cb.data = "bl:sp:0"
    await samples_list(cb)


@router.callback_query(F.data == "bl:spadd")
async def sample_add(cb: CallbackQuery, state: FSMContext) -> None:
    await ask(cb, state, "bl_sample", "Пришлите текст спама целиком (можно переслать сообщение). "
              f"Похожие с любых аккаунтов перестанут быть заявками. Нужно хотя бы {spamtexts.MIN_WORDS} слов.")


async def bl_sample(s, m: Message, d, text):
    sample = await spamtexts.remember(s, text, added_by=m.from_user.id)
    if sample is None:
        await s.rollback()
        return await m.answer("Слишком короткий текст или похожий уже есть в списке.",
                              reply_markup=kb([[("🧩 Спам-тексты", "bl:sp:0")]]))
    await s.commit()
    await m.answer(f"✔ Спам-текст №{sample.id} сохранён.", reply_markup=kb([[("🧩 Спам-тексты", "bl:sp:0")]]))


# ---------- отсеянные как реклама услуг ----------
@router.callback_query(F.data.regexp(r"^bl:fl:\d{1,6}$"))
async def filtered_list(cb: CallbackQuery) -> None:
    offset = int(cb.data.split(":")[2])
    async with Session() as s:
        total = await s.scalar(select(func.count()).select_from(FilteredMessage)) or 0
        rows_ = (await s.scalars(select(FilteredMessage).order_by(FilteredMessage.id.desc()).offset(offset).limit(PAGE))).all()
    rows = [[(f"{' '.join(x.text.split())[:45]}", f"bl:fo:{x.id}")] for x in rows_]
    nav = []
    if offset:
        nav.append(("⬅️", f"bl:fl:{max(offset - PAGE, 0)}"))
    if offset + PAGE < total:
        nav.append(("➡️", f"bl:fl:{offset + PAGE}"))
    if nav:
        rows.append(nav)
    rows.append([("◀️ Назад", "bl:menu")])
    await reply(cb, f"🙅 Отсеяно как реклама услуг (за 3 дня): {total}\nНажмите, чтобы посмотреть и, если это заказчик, вернуть."
                + ("" if rows_ else "\nПока пусто."), kb(rows))


@router.callback_query(F.data.regexp(r"^bl:fo:\d{1,9}$"))
async def filtered_open(cb: CallbackQuery) -> None:
    async with Session() as s:
        x = await s.get(FilteredMessage, int(cb.data.split(":")[2]))
    if x is None:
        return await cb.answer("Уже обработано", show_alert=True)
    who = f"@{x.username}" if x.username else (x.first_name or str(x.author_id))
    link = f'\n<a href="{esc(x.link)}">Ссылка на сообщение</a>' if x.link else ""
    await reply(cb, f"🙅 Отсеяно: {esc(x.reason)}\nГруппа: {esc(x.group_title)}\nКто: {esc(who)}{link}\n\n{esc(x.text[:1500])}",
                kb([[("↩️ Это заявка — раздать", f"bl:fok:{x.id}")], [("🗑 Убрать из списка", f"bl:fdel:{x.id}")],
                    [("◀️ К списку", "bl:fl:0")]]))


@router.callback_query(F.data.regexp(r"^bl:fok:\d{1,9}$"))
async def filtered_restore(cb: CallbackQuery) -> None:
    """Фильтр ошибся: это заказчик. Пропускаем сообщение через обычный путь заявки, но без фильтра «реклама услуг»."""
    from app.services import intake
    from app.services.antispam import AuthorInfo
    from app.services.pipeline import Incoming

    async with Session() as s:
        x = await s.get(FilteredMessage, int(cb.data.split(":")[2]))
        if x is None:
            return await cb.answer("Уже обработано", show_alert=True)
        msg = Incoming(text=x.text, author=AuthorInfo(user_id=x.author_id, username=x.username, first_name=x.first_name),
                       group_tg_id=x.group_tg_id, group_title=x.group_title, message_id=x.message_id, link=x.link)
        from app.services import offers

        await offers.learn(s, x.text, "request")  # фильтр запомнит: похожие — заявки
        await s.delete(x)
        await s.commit()
    lead = await intake.handle(msg, force=True)
    await cb.answer("Заявка разослана" if lead else "Не стала заявкой (дубль или спам)", show_alert=True)
    cb.data = "bl:fl:0"
    await filtered_list(cb)


@router.callback_query(F.data.regexp(r"^bl:fdel:\d{1,9}$"))
async def filtered_delete(cb: CallbackQuery) -> None:
    async with Session() as s:
        x = await s.get(FilteredMessage, int(cb.data.split(":")[2]))
        if x is not None:
            await s.delete(x)
            await s.commit()
    await cb.answer("Убрано")
    cb.data = "bl:fl:0"
    await filtered_list(cb)


# ---------- обучение фильтра «заказчик или исполнитель» ----------
@router.callback_query(F.data.regexp(r"^bl:lb:\d{1,6}$"))
async def labeled_list(cb: CallbackQuery) -> None:
    offset = int(cb.data.split(":")[2])
    async with Session() as s:
        total = await s.scalar(select(func.count()).select_from(LabeledText)) or 0
        rows_ = (await s.scalars(select(LabeledText).order_by(LabeledText.id.desc()).offset(offset).limit(PAGE))).all()
    rows = [[(("🙅 " if x.kind == "offer" else "✅ ") + " ".join(x.text.split())[:42], f"bl:lbo:{x.id}")] for x in rows_]
    nav = []
    if offset:
        nav.append(("⬅️", f"bl:lb:{max(offset - PAGE, 0)}"))
    if offset + PAGE < total:
        nav.append(("➡️", f"bl:lb:{offset + PAGE}"))
    if nav:
        rows.append(nav)
    rows.append([("◀️ Назад", "bl:menu")])
    await reply(cb, f"🎓 Ваши отметки: {total}\n🙅 — исполнитель (похожие отсеиваются), ✅ — заказчик (похожие доходят).\n"
                "Появляются, когда вы жмёте «🙅 Исполнитель» под заявкой или «↩️ Это заявка» в отсеянных. Нажмите, чтобы удалить ошибочную."
                + ("" if rows_ else "\nПока пусто."), kb(rows))


@router.callback_query(F.data.regexp(r"^bl:lbo:\d{1,9}$"))
async def labeled_open(cb: CallbackQuery) -> None:
    async with Session() as s:
        x = await s.get(LabeledText, int(cb.data.split(":")[2]))
    if x is None:
        return await cb.answer("Уже удалено", show_alert=True)
    await reply(cb, ("🙅 Исполнитель" if x.kind == "offer" else "✅ Заказчик") + f" (отмечено {x.created_at:%d.%m})\n\n{esc(x.text[:1500])}",
                kb([[("🗑 Удалить отметку", f"bl:lbd:{x.id}")], [("◀️ К списку", "bl:lb:0")]]))


@router.callback_query(F.data.regexp(r"^bl:lbd:\d{1,9}$"))
async def labeled_delete(cb: CallbackQuery) -> None:
    async with Session() as s:
        x = await s.get(LabeledText, int(cb.data.split(":")[2]))
        if x is not None:
            await s.delete(x)
            await s.commit()
    await cb.answer("Удалено — фильтр больше не опирается на этот пример")
    cb.data = "bl:lb:0"
    await labeled_list(cb)
