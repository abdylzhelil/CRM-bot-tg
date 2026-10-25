from datetime import timedelta

from aiogram import F, Router
from aiogram.types import Message
from sqlalchemy import case, func, select

from app.bot.common import esc
from app.bot.handlers.leads import category_name
from app.db import Session
from app.models import STAGE_NAMES, Client, Delivery, Group, Lead, Payment, User, now

router = Router()


async def build_report() -> str:
    async with Session() as s:
        day, week = now() - timedelta(days=1), now() - timedelta(days=7)
        count = lambda *w: s.scalar(select(func.count()).select_from(Lead).where(*w))  # noqa: E731
        today, wk, total = await count(Lead.created_at > day), await count(Lead.created_at > week), await count()
        taken = await s.scalar(select(func.count()).select_from(Delivery).where(
            Delivery.status == "taken", Delivery.created_at > day))
        stages = dict((await s.execute(select(Client.stage, func.count()).group_by(Client.stage))).all())
        done_sum = await s.scalar(select(func.coalesce(func.sum(Client.budget), 0)).where(Client.stage == "done"))
        cats = (await s.execute(
            select(Lead.category_id, func.count()).where(Lead.created_at > week)
            .group_by(Lead.category_id).order_by(func.count().desc()).limit(5)
        )).all()
        groups = (await s.scalars(select(Group).where(Group.leads_count > 0).order_by(Group.leads_count.desc()).limit(5))).all()
        lines = [
            f"📊 Найдено заявок: за сутки {today}, за неделю {wk}, всего {total}; взято в работу за сутки: {taken}",
            "Клиенты: " + (", ".join(f"{STAGE_NAMES[k]} {v}" for k, v in stages.items()) or "нет"),
            f"💰 Сумма выполненных: {done_sum}",
        ]
        if cats:
            lines.append("\nПо категориям (неделя):")
            lines += [f"• {esc(await category_name(s, cid))}: {n}" for cid, n in cats]
        if groups:
            lines.append("\n🏆 Рейтинг групп:")
            lines += [f"• {esc(g.title)}: {g.leads_count}" for g in groups]
    return "\n".join(lines)


async def user_stats(user_id: int) -> dict:
    """Личные цифры пользователя (для бота и кабинета)."""
    async with Session() as s:
        day, week = now() - timedelta(days=1), now() - timedelta(days=7)
        mine = Delivery.user_id == user_id
        count = lambda *w: s.scalar(select(func.count()).select_from(Delivery).where(mine, Delivery.sent, *w))  # noqa: E731
        today, wk, total = await count(Delivery.created_at > day), await count(Delivery.created_at > week), await count()
        taken = await count(Delivery.status == "taken")
        stages = dict((await s.execute(
            select(Client.stage, func.count()).where(Client.user_id == user_id).group_by(Client.stage)
        )).all())
        done_sum = await s.scalar(select(func.coalesce(func.sum(Client.budget), 0)).where(
            Client.user_id == user_id, Client.stage == "done"))
    return {"today": today, "week": wk, "total": total, "taken": taken, "stages": stages, "done_sum": int(done_sum or 0),
            "conversion": round(taken / total * 100) if total else None}


async def user_report(user_id: int) -> str:
    st = await user_stats(user_id)
    conv = f"{st['conversion']}%" if st["conversion"] is not None else "—"
    return "\n".join([
        f"📊 Ваши заявки: за сутки {st['today']}, за неделю {st['week']}, всего {st['total']}",
        f"Взято в работу: {st['taken']} (конверсия {conv})",
        "Клиенты: " + (", ".join(f"{STAGE_NAMES[k]} {v}" for k, v in st["stages"].items()) or "пока нет"),
        f"💰 Сумма выполненных сделок: {st['done_sum']}",
    ])


async def users_line() -> str:
    async with Session() as s:
        total = await s.scalar(select(func.count()).select_from(User))
        active = await s.scalar(select(func.count()).select_from(User).where(User.last_seen > now() - timedelta(days=7)))
        paid = await s.scalar(select(func.count()).select_from(User).where(User.plan == "full"))
        ready = await s.scalar(select(func.count()).select_from(User).where(User.onboarded)) or 0
        unverified = await s.scalar(select(func.count()).select_from(User).where(~User.verified)) or 0
        pay = (await s.execute(select(func.count(), func.coalesce(func.sum(Payment.amount - Payment.refunded_amount), 0)).where(
            Payment.created_at > now() - timedelta(days=30)))).one()
        back = (await s.execute(select(func.count(), func.coalesce(func.sum(Payment.refunded_amount), 0)).where(
            Payment.created_at > now() - timedelta(days=30), Payment.refunded_amount > 0))).one()
    money = f"\n💰 Оплаты за 30 дней: {pay[0]} шт. на {pay[1] / 100:.0f} ₽ (уже за вычетом возвратов)" if pay[0] else ""
    if back[0]:
        money += (f"\n↩️ Возвраты по этим оплатам: {back[0]} шт. на {back[1] / 100:.0f} ₽ "
                  "(комиссия ЮKassa за них не возвращается)")
    async with Session() as s:
        from app.services import credits

        c_total, c_holders, c_earned = await credits.totals(s)
    if c_earned or c_total:
        money += f"\n💰 Кредиты: на счетах {c_total} у {c_holders} чел., начислено всего {c_earned}"
    empty = total - ready
    extra = f"\n🧹 Не завершили регистрацию: {empty}" + (f" (не прошли проверку: {unverified})" if unverified else "") if empty else ""
    async with Session() as s:
        from app.services import ai

        calls, tin, tout = await ai.usage_summary(s)
    ai_line = f"\n✨ ИИ за 30 дней: {calls} ответов, токенов: {tin:,} вход / {tout:,} выход".replace(",", " ") if calls else ""
    return f"👥 Пользователей: {ready} (зарегистрированы), активных за неделю: {active}, на полном тарифе: {paid}{extra}{money}{ai_line}"


async def funnel_text() -> str:
    """Откуда теряются люди: зашли → прошли проверку → выбрали ниши → завершили регистрацию → оплатили."""
    from app.models import Payment, UserCategory

    async with Session() as s:
        count = lambda *w: s.scalar(select(func.count()).select_from(User).where(User.is_admin == False, *w))  # noqa: E711,E731
        entered = await count()
        verified = await count(User.verified)
        chose = await s.scalar(select(func.count(func.distinct(UserCategory.user_id))).select_from(UserCategory)
                               .join(User, User.tg_id == UserCategory.user_id).where(User.is_admin == False))  # noqa: E712
        done = await count(User.onboarded)
        referred = await count(User.onboarded, User.referred_by != 0)
        buyers = await s.scalar(select(func.count(func.distinct(Payment.user_id))).where(~Payment.refunded)) or 0
        sources = (await s.execute(select(User.source, func.count(), func.sum(case((User.onboarded, 1), else_=0)))
                                   .where(User.is_admin == False, User.source != "")  # noqa: E712
                                   .group_by(User.source).order_by(func.count().desc()).limit(8))).all()
    if not entered:
        return ""
    src = ("\nПо меткам ссылок (зашли / зарегистрировались): "
           + ", ".join(f"{name} {n}/{int(done_n or 0)}" for name, n, done_n in sources)) if sources else ""
    return ("\n\n<b>Воронка</b>\n"
            f"Зашли: {entered} → прошли проверку: {verified} → выбрали ниши: {chose or 0} → "
            f"завершили регистрацию: {done} → оплатили: {buyers}\n"
            f"Из завершивших по приглашению: {referred}" + src)


@router.message(F.text == "📊 Статистика")
async def stats(m: Message, user: User) -> None:
    text = await user_report(user.tg_id)
    if user.is_admin:
        text += "\n\n<b>Весь бот</b>\n" + await users_line() + "\n" + await build_report() + await funnel_text()
    from app.bot.common import kb

    await m.answer(text, reply_markup=kb([[("🏆 Заявки по нишам в моём городе", "rt:menu")]]))
