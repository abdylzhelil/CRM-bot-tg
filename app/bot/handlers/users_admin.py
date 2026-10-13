"""Администратор: пользователи бота и их тарифы."""
from datetime import timedelta

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select

from app.bot.common import ask, esc, kb, reply
from app.db import Session
from app.models import PLANS, Category, Client, Delivery, User, UserCategory, now
from app.services.distribute import delivered_today, is_paid

router = Router()


@router.message(F.text == "👥 Пользователи")
async def users_menu(target: Message | CallbackQuery) -> None:
    from app.bot.handlers.stats import users_line

    async with Session() as s:
        users = (await s.scalars(select(User).order_by(User.last_seen.desc()).limit(25))).all()
    rows = [[(("💎 " if is_paid(u) else "") + ("⛔ " if u.blocked else "") + ("⚪ " if not u.onboarded else "") + (u.name or str(u.tg_id))[:40]
              + (f" · {u.city}" if u.city else ""), f"us:open:{u.tg_id}")] for u in users]
    rows.append([("🔎 Найти по ID или @нику", "us:find")])
    rows.append([("🧹 Убрать пустые аккаунты", "us:clean")])
    await reply(target, await users_line() + "\nПоследние активные:", kb(rows))


@router.callback_query(F.data == "us:menu")
async def back(cb: CallbackQuery) -> None:
    await users_menu(cb)


@router.callback_query(F.data == "us:find")
async def find(cb: CallbackQuery, state: FSMContext) -> None:
    await ask(cb, state, "us_find", "Telegram ID или @username пользователя:")


async def user_card(target, tg_id: int) -> None:
    async with Session() as s:
        u = await s.get(User, tg_id)
        if u is None:
            return await reply(target, "Пользователь не найден", kb([[("◀️ Назад", "us:menu")]]))
        niches = (await s.scalars(select(Category.name).join(UserCategory, UserCategory.category_id == Category.id)
                                  .where(UserCategory.user_id == tg_id))).all()
        total = await s.scalar(select(func.count()).select_from(Delivery).where(Delivery.user_id == tg_id))
        today = await delivered_today(s, tg_id)
        clients = await s.scalar(select(func.count()).select_from(Client).where(Client.user_id == tg_id))
    plan = PLANS["full" if is_paid(u) else "free"]
    until = f" до {u.plan_until:%d.%m.%Y}" if is_paid(u) and u.plan_until else (" бессрочно" if is_paid(u) else "")
    text = (
        f"👤 <b>{esc(u.name or '—')}</b> {('@' + esc(u.username)) if u.username else ''}\n"
        f"ID: <code>{u.tg_id}</code>{' · администратор' if u.is_admin else ''}\n"
        f"Тариф: {plan}{until}\nГород: {esc(u.city or 'вся Россия')}\nНиши: {esc(', '.join(niches)) or '—'}\n"
        f"Заявок: сегодня {today}, всего {total} · клиентов в CRM: {clients}\n"
        f"С нами с {u.created_at:%d.%m.%Y}, был(а) {u.last_seen:%d.%m %H:%M} UTC"
        f"{chr(10) + '⛔ Бот заблокирован пользователем' if u.blocked else ''}"
    )
    await reply(target, text, kb([
        [("💎 +30 дней", f"us:plan:{tg_id}:30"), ("💎 +365 дней", f"us:plan:{tg_id}:365")],
        [("💎 Бессрочно", f"us:plan:{tg_id}:0"), ("🆓 Бесплатный", f"us:plan:{tg_id}:free")],
        [("◀️ К списку", "us:menu")],
    ]))


@router.callback_query(F.data.startswith("us:open:"))
async def open_user(cb: CallbackQuery) -> None:
    await user_card(cb, int(cb.data.split(":")[2]))


@router.callback_query(F.data.startswith("us:plan:"))
async def set_plan(cb: CallbackQuery) -> None:
    _, _, raw_id, value = cb.data.split(":")
    async with Session() as s:
        u = await s.get(User, int(raw_id))
        if u is None:
            return await cb.answer("Не найден")
        if value == "free":
            u.plan, u.plan_until = "free", None
            notice = "Ваш тариф изменён на 🆓 Бесплатный."
        else:
            days = int(value)
            start = u.plan_until if is_paid(u) and u.plan_until and u.plan_until > now() else now()
            u.plan, u.plan_until = "full", (start + timedelta(days=days)) if days else None
            notice = "🎉 Подключён 💎 Полный тариф" + (f" до {u.plan_until:%d.%m.%Y}" if u.plan_until else " бессрочно") \
                + ": заявки приходят сразу и без лимита."
        await s.commit()
    try:
        await cb.bot.send_message(int(raw_id), notice)
    except Exception:
        pass
    await cb.answer("Тариф обновлён")
    await user_card(cb, int(raw_id))


# ---------- чистка «пустых» аккаунтов ----------
async def empty_users(s, days: int = 3) -> list[int]:
    """Не завершили регистрацию давно, заявок и клиентов нет, не администраторы."""
    cutoff = now() - timedelta(days=days)
    users = (await s.scalars(select(User).where(~User.onboarded, ~User.is_admin, User.created_at < cutoff))).all()
    out = []
    for x in users:
        has = await s.scalar(select(func.count()).select_from(Delivery).where(Delivery.user_id == x.tg_id)) or 0
        has += await s.scalar(select(func.count()).select_from(Client).where(Client.user_id == x.tg_id)) or 0
        if not has:
            out.append(x.tg_id)
    return out


@router.callback_query(F.data == "us:clean")
async def clean_ask(cb: CallbackQuery) -> None:
    async with Session() as s:
        ids = await empty_users(s)
    if not ids:
        return await cb.answer("Пустых аккаунтов нет (старше 3 дней)", show_alert=True)
    await reply(cb, f"🧹 Найдено пустых аккаунтов: {len(ids)}\nЭто люди, которые зашли, но за 3 дня не завершили регистрацию "
                "и ничего не получали. Удалить их из базы? Если кто-то вернётся, просто пройдёт регистрацию заново.",
                kb([[(f"✅ Удалить {len(ids)}", "us:clean:do"), ("❌ Не надо", "us:menu")]]))


@router.callback_query(F.data == "us:clean:do")
async def clean_do(cb: CallbackQuery) -> None:
    async with Session() as s:
        ids = await empty_users(s)
        if ids:
            await s.execute(User.__table__.delete().where(User.tg_id.in_(ids)))
            await s.commit()
    await cb.answer(f"Удалено: {len(ids)}", show_alert=True)
    await users_menu(cb)
