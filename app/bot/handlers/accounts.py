"""Добавление аккаунтов-разведчиков командой из бота — без правки кода."""
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from telethon.errors import PhoneCodeInvalidError, SessionPasswordNeededError

from app import config
from app.bot.common import AccountFlow, ask, esc, kb, reply
from app.db import Session
from app.models import ScoutAccount
from app.scout.client import make_client
from app.services import settings

router = Router()
manager = None  # подставляется из main.py
# Незавершённые входы: {owner_id: (client, acc_row, phone_code_hash)}
_pending: dict[int, tuple] = {}
ICON = {"ok": "🟢", "new": "⚪️", "limited": "🟠", "dead": "🔴"}


@router.callback_query(F.data == "acc:list")
async def accounts(cb: CallbackQuery) -> None:
    async with Session() as s:
        accs = (await s.scalars(select(ScoutAccount).order_by(ScoutAccount.id))).all()
    rows = [[(f"{ICON.get(a.status, '⚪️')} {a.name} · {a.joined_today}/{a.daily_join_limit}", f"acc:open:{a.id}")] for a in accs]
    rows.append([("➕ Добавить аккаунт", "acc:add"), ("◀️ Назад", "st:menu")])
    await reply(cb, "👤 Аккаунты-разведчики (🟢 работает, 🔴 вылетел). Разведчик — отдельный номер, не основной!", kb(rows))


@router.callback_query(F.data.startswith("acc:open:"))
async def open_acc(cb: CallbackQuery) -> None:
    async with Session() as s:
        a = await s.get(ScoutAccount, int(cb.data.split(":")[2]))
    text = (
        f"<b>{esc(a.name)}</b> {ICON.get(a.status, '')} {a.status}\nТелефон: {esc(a.phone)}\n"
        f"Вступлений сегодня: {a.joined_today}/{a.daily_join_limit}\n"
        f"Лимит групп: {a.max_groups}\nВключён: {'да' if a.enabled else 'нет'}\n"
        f"Публикует рекламу: {'да' if a.can_post else 'нет'}"
    )
    await reply(cb, text, kb([
        [("⏯ Вкл/выкл", f"acc:tog:{a.id}"), ("🔁 Перезапустить", f"acc:restart:{a.id}")],
        [("📈 Лимит в сутки", f"acc:limit:{a.id}")],
        [(("🔇 Не публиковать рекламу" if a.can_post else "📣 Разрешить публиковать рекламу"), f"acc:post:{a.id}")],
        [("📁 Разложить группы по папкам", f"acc:folders:{a.id}")],
        [("◀️ Назад", "acc:list")],
    ]))


@router.callback_query(F.data.regexp(r"^acc:(tog|restart):\d+$"))
async def toggle(cb: CallbackQuery) -> None:
    _, op, aid = cb.data.split(":")
    async with Session() as s:
        a = await s.get(ScoutAccount, int(aid))
        if op == "tog":
            a.enabled = not a.enabled
            await s.commit()
    if manager:
        if a.enabled:
            await manager.attach(a)
        else:
            await manager.detach(a.id)
    await open_acc(cb)


@router.callback_query(F.data.startswith("acc:post:"))
async def toggle_post(cb: CallbackQuery) -> None:
    async with Session() as s:
        a = await s.get(ScoutAccount, int(cb.data.split(":")[2]))
        a.can_post = not a.can_post
        await s.commit()
    await open_acc(cb)


@router.callback_query(F.data.startswith("acc:folders:"))
async def folders(cb: CallbackQuery) -> None:
    from app.scout.folders import sync_folders

    aid = int(cb.data.split(":")[2])
    client = manager.clients.get(aid) if manager else None
    if client is None:
        return await cb.answer("Разведчик не подключён", show_alert=True)
    await cb.answer("Раскладываю…")
    count = await sync_folders(client, aid)
    await cb.message.answer(f"📁 Готово: {count} групп в папках «🔍 Разведка» у аккаунта разведчика.")


@router.callback_query(F.data.regexp(r"^acc:limit:\d+$"))
async def edit(cb: CallbackQuery, state: FSMContext) -> None:
    aid = cb.data.split(":")[2]
    await ask(cb, state, "acc_limit", "Лимит вступлений в сутки (10–20 безопасно):", id=int(aid))


# ---- пошаговое добавление ----
@router.callback_query(F.data == "acc:add")
async def add(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AccountFlow.name)
    await cb.message.answer("Имя аккаунта (для вас, напр. «scout1»):\n\n/cancel — отмена")
    await cb.answer()


@router.message(AccountFlow.name)
async def f_name(m: Message, state: FSMContext) -> None:
    await state.update_data(name=m.text.strip())
    await state.set_state(AccountFlow.api_id)
    await m.answer("api_id (my.telegram.org) или «-», чтобы взять из .env:")


@router.message(AccountFlow.api_id)
async def f_api_id(m: Message, state: FSMContext) -> None:
    raw = m.text.strip()
    if raw == "-":
        await state.update_data(api_id=config.TG_API_ID, api_hash=config.TG_API_HASH)
        await state.set_state(AccountFlow.phone)
        return await m.answer("Номер телефона разведчика (+7…):")
    if not raw.isdigit():
        return await m.answer("api_id — число. Повторите:")
    await state.update_data(api_id=int(raw))
    await state.set_state(AccountFlow.api_hash)
    await m.answer("api_hash:")


@router.message(AccountFlow.api_hash)
async def f_api_hash(m: Message, state: FSMContext) -> None:
    await state.update_data(api_hash=m.text.strip())
    await state.set_state(AccountFlow.phone)
    await m.answer("Номер телефона разведчика (+7…):")


@router.message(AccountFlow.phone)
async def f_phone(m: Message, state: FSMContext) -> None:
    await state.update_data(phone=m.text.strip())
    data = await state.get_data()
    async with Session() as s:
        limit = await settings.get_int(s, "default_join_limit")
        acc = ScoutAccount(
            name=data["name"], api_id=data["api_id"], api_hash=data["api_hash"],
            phone=data["phone"], daily_join_limit=limit,
        )
        s.add(acc)
        try:
            await s.commit()
        except Exception:
            await state.clear()
            return await m.answer("Не удалось сохранить (имя занято?). Начните заново.")
    try:
        client = make_client(acc, session="")
        await client.connect()
        sent = await client.send_code_request(acc.phone)
    except Exception as e:
        await state.clear()
        return await m.answer(f"Не удалось отправить код: {esc(str(e))}")
    _pending[m.from_user.id] = (client, acc, sent.phone_code_hash)
    await state.set_state(AccountFlow.code)
    await m.answer("Код отправлен в Telegram на этот номер. Введите его (можно с пробелами/точками, "
                   "чтобы Telegram не заблокировал код: 1 2 3 4 5):")


async def _finish(m: Message, state: FSMContext, client, acc: ScoutAccount) -> None:
    async with Session() as s:
        row = await s.get(ScoutAccount, acc.id)
        row.session = client.session.save()
        row.status = "ok"
        await s.commit()
    await client.disconnect()
    _pending.pop(m.from_user.id, None)
    await state.clear()
    if manager:
        await manager.attach(row)
    await m.answer(f"✅ Аккаунт «{esc(acc.name)}» подключён и начал работу.")


@router.message(AccountFlow.code)
async def f_code(m: Message, state: FSMContext) -> None:
    client, acc, code_hash = _pending[m.from_user.id]
    code = "".join(ch for ch in m.text if ch.isdigit())
    try:
        await client.sign_in(acc.phone, code, phone_code_hash=code_hash)
    except SessionPasswordNeededError:
        await state.set_state(AccountFlow.password)
        return await m.answer("Включена двухфакторка. Введите облачный пароль:")
    except PhoneCodeInvalidError:
        return await m.answer("Неверный код. Повторите:")
    except Exception as e:
        await state.clear()
        return await m.answer(f"Ошибка входа: {esc(str(e))}")
    await _finish(m, state, client, acc)


@router.message(AccountFlow.password)
async def f_password(m: Message, state: FSMContext) -> None:
    client, acc, _ = _pending[m.from_user.id]
    try:
        await client.sign_in(password=m.text.strip())
    except Exception as e:
        return await m.answer(f"Неверный пароль: {esc(str(e))}. Повторите или /cancel")
    await m.delete()  # не оставляем пароль в чате
    await _finish(m, state, client, acc)
