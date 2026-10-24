"""Администратор: пользователи бота и их тарифы."""
from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select

from app.bot.common import ask, esc, kb, reply
from app.db import Session
from app.models import PLANS, Category, Client, Delivery, User, UserCategory
from app.services import retention
from app.services import users_admin as users_svc
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
    rows.append([("🗑 Удалить данные автора заявки", "us:forget")])
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
        f"Тариф: {plan}{until}\n💰 Кредиты: {u.credits or 0}\nГород: {esc(u.city or 'вся Россия')}\nНиши: {esc(', '.join(niches)) or '—'}\n"
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
        notice = users_svc.set_plan(u, value)
        await s.commit()
    try:
        await cb.bot.send_message(int(raw_id), notice)
    except Exception:
        pass
    await cb.answer("Тариф обновлён")
    await user_card(cb, int(raw_id))


# ---------- чистка «пустых» аккаунтов ----------
async def empty_users(s, days: int = 3) -> list[int]:
    from app.services import cleanup

    return [x.tg_id for x in await cleanup.empty_users(s, days)]


@router.callback_query(F.data == "us:clean")
async def clean_ask(cb: CallbackQuery) -> None:
    async with Session() as s:
        ids = await empty_users(s)
    if not ids:
        return await cb.answer("Пустых аккаунтов нет (старше 3 дней)", show_alert=True)
    await reply(cb, f"🧹 Найдено пустых аккаунтов: {len(ids)}\nЭто люди, которые зашли, но за 3 дня не завершили регистрацию "
                "и ничего не получали. Удалить их из базы? Если кто-то вернётся, просто пройдёт регистрацию заново.\n"
                "(Автоматически бот удаляет таких через 7 дней — срок меняется в ⚙️ Настройки → 🛡 Антиспам.)",
                kb([[(f"✅ Удалить {len(ids)}", "us:clean:do"), ("❌ Не надо", "us:menu")]]))


@router.callback_query(F.data == "us:clean:do")
async def clean_do(cb: CallbackQuery) -> None:
    from app.services import cleanup

    async with Session() as s:
        ids = await empty_users(s)
        await cleanup.remove(s, ids)
    await cb.answer(f"Удалено: {len(ids)}", show_alert=True)
    await users_menu(cb)


# ---------- удаление данных автора заявки (по его просьбе) ----------
async def forget_preview(target, query: str) -> None:
    """Показывает, что будет удалено, и просит подтвердить. Само удаление — только по кнопке."""
    async with Session() as s:
        authors = await retention.find_authors(s, query)
        if not authors:
            return await reply(target, "Автор не найден среди заявок и чёрного списка.", kb([[("◀️ Назад", "us:menu")]]))
        rows, lines = [], []
        for author_id, uname in authors[:5]:
            st = await retention.author_stats(s, author_id)
            if st["is_user"]:
                lines.append(f"• <code>{author_id}</code> {('@' + esc(uname)) if uname else ''} — пользователь бота: его данные удаляет команда /deleteme у него.")
                continue
            lines.append(f"• <code>{author_id}</code> {('@' + esc(uname)) if uname else ''}: заявок {st['leads']}, доставок {st['deliveries']}, "
                         f"карточек в CRM {st['crm']} (обезличим), чёрный список: {'да' if st['blacklist'] else 'нет'}")
            rows.append([(f"🗑 Удалить {author_id}", f"fg:do:{author_id}")])
    rows.append([("Отмена", "us:menu")])
    await reply(target, "🗑 <b>Удалить данные автора?</b>\n" + "\n".join(lines)
                + "\n\nБудут удалены его заявки (тексты, имя, ник), голоса «спам» и запись в чёрном списке. "
                "Если он писал спам, после удаления бот снова сможет принимать его сообщения.", kb(rows))


@router.message(Command("forget"))
async def forget_cmd(m: Message, command: CommandObject) -> None:
    if not (command.args or "").strip():
        return await m.answer("Напишите так: /forget @ник или /forget ID автора заявки. Удалю его заявки и данные (с вашим подтверждением).")
    await forget_preview(m, command.args)


@router.callback_query(F.data == "us:forget")
async def forget_ask(cb: CallbackQuery, state: FSMContext) -> None:
    await ask(cb, state, "us_forget", "ID или @ник автора заявки, данные которого нужно удалить:")


@router.callback_query(F.data.regexp(r"^fg:do:\d+$"))
async def forget_do(cb: CallbackQuery) -> None:
    author_id = int(cb.data.split(":")[2])
    async with Session() as s:
        if await s.get(User, author_id) is not None:
            return await cb.answer("Это пользователь бота — удаляйте через /deleteme у него", show_alert=True)
        st = await retention.forget_author(s, author_id)
    await reply(cb, f"✅ Удалено: заявок {st['leads']}, доставок {st['deliveries']}; карточек CRM обезличено: {st['crm']}"
                f"{'; запись в чёрном списке удалена' if st['blacklist'] else ''}.", kb([[("◀️ К пользователям", "us:menu")]]))
    await cb.answer("Готово")


# ---------- демо привязанной карты — только чтобы снять скриншоты интерфейса для ЮKassa ----------
@router.message(Command("demo_card"))
async def demo_card(m: Message, command: CommandObject) -> None:
    """/demo_card — показать у себя экран привязанной карты (без реальной карты и списаний); /demo_card off — убрать.

    Автосписания у администраторов не выполняются никогда (renew_due пропускает админов), реальных денег это не трогает.
    """
    from app.services import payments

    off = (command.args or "").strip().lower() in ("off", "выкл", "0")
    async with Session() as s:
        u = await s.get(User, m.from_user.id)
        if off:
            payments.disable_renew(u)
            text = "Демо-карта убрана."
        else:
            u.renew_method, u.renew_card, u.renew_days = "demo", "Mir •••• 4242", 30
            text = ("Демо включено (карты и списаний нет, это только экран). Откройте: 👤 Профиль → 💎 Тариф — там «Привязанная карта» "
                    "и кнопка «🗑 Отвязать карту». Снимите полноэкранные скриншоты, потом отправьте /demo_card off")
        await s.commit()
    await m.answer(text)
