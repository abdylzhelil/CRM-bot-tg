import asyncio
import logging

from sqlalchemy import select

from app import config
from app.bot import notify
from app.bot.handlers import payments as payments_h
from app.bot.handlers.stats import build_report
from app.bot.setup import build, setup_profile
from app.db import Session, engine, init_db
from app.migrate import migrate_data, upgrade_schema
from app.feed import server as feed_server
from app.models import Client, Reminder, now
from app.services import backup, public_stats, quiet, settings
from app.scout.channels import channel_loop
from app.scout.joiner import join_loop, rotation_loop, search_loop
from app.scout.manager import ScoutManager
from app.seed import seed

log = logging.getLogger("crm")
REPEAT_TOUCH = "Вы ещё рассматриваете наше предложение? Могу ответить на вопросы."


async def send_to(user_id: int, text: str, markup=None) -> None:
    try:
        await notify._bot.send_message(user_id, text, reply_markup=markup, disable_web_page_preview=True)
    except Exception:
        log.info("не удалось написать %s", user_id)


async def reminder_loop() -> None:
    """Напоминания «дожать» (владельцу клиента) и вечерний отчёт администраторам."""
    last_report = None
    while True:
        try:
            async with Session() as s:
                due = (await s.scalars(select(Reminder).where(~Reminder.sent, Reminder.due_at <= now()))).all()
                for r in due:
                    c = await s.get(Client, r.client_id)
                    r.sent = True
                    if c is None:
                        continue
                    who = c.name or c.username or c.tg_id
                    await send_to(
                        c.user_id or min(config.OWNER_IDS),
                        f"⏰ {r.text}: {who}\nМожно отправить: «{REPEAT_TOUCH}»",
                        notify.InlineKeyboardMarkup(inline_keyboard=[[
                            notify.InlineKeyboardButton(text="👤 Открыть", callback_data=f"cl:open:{c.id}")]]),
                    )
                await s.commit()
            t = quiet.local_now()
            async with Session() as s:
                report_hour = await settings.get_int(s, "report_hour")
            if t.hour == report_hour and last_report != t.date():
                last_report = t.date()
                await notify._send("🌙 Вечерний отчёт\n" + await build_report())
        except Exception:
            log.exception("reminder_loop")
            await notify.alert_once("reminder_loop", "В фоновой задаче reminder_loop ошибка (см. логи сервера).")
        await asyncio.sleep(60)


async def delivery_loop() -> None:
    """Заявки с задержкой (бесплатный тариф) и накопленные за режим тишины."""
    while True:
        try:
            await notify.flush_due()
        except Exception:
            log.exception("delivery_loop")
            await notify.alert_once("delivery_loop", "В фоновой задаче delivery_loop ошибка (см. логи сервера).")
        await asyncio.sleep(20)


async def expiry_once() -> int:
    """Тарифы 💎: за 3 дня до конца — напоминание (или предупреждение о списании), в день конца — «тариф закончился».

    Возвращает число отправленных сообщений.
    """
    from datetime import timedelta

    from app.models import User
    from app.services import payments

    sent = 0
    button = notify.InlineKeyboardMarkup(inline_keyboard=[[
        notify.InlineKeyboardButton(text="💎 Продлить", callback_data="pf:plan")]])
    async with Session() as s:
        soon = (await s.scalars(select(User).where(
            User.plan == "full", User.plan_until.is_not(None), ~User.blocked,
            User.plan_until > now(), User.plan_until < now() + timedelta(days=3)))).all()
        for u in soon:
            if u.expiry_notice == u.plan_until:
                continue
            u.expiry_notice = u.plan_until
            sent += 1
            price = await payments.price_of(s, u.renew_days) if u.renew_method else 0
            if u.renew_method and price:
                text = (f"🔁 💎 Полный тариф заканчивается {u.plan_until:%d.%m}: автоматически спишем {price} ₽ "
                        f"за продление на {u.renew_days} дн. Не нужно — отключите: 👤 Профиль → 💎 Тариф.")
                await send_to(u.tg_id, text)
            else:
                await send_to(u.tg_id, f"⏳ 💎 Полный тариф заканчивается {u.plan_until:%d.%m}. "
                              "Продлите, чтобы заявки приходили сразу и без лимита.", button)
        ended = (await s.scalars(select(User).where(
            User.plan == "full", User.plan_until.is_not(None), User.plan_until <= now(), ~User.is_admin))).all()
        for u in ended:
            if u.renew_method and u.renew_for != u.plan_until:
                continue  # автопродление ещё попробует — раньше времени «закончился» не пишем
            u.plan, u.plan_until, u.expiry_notice = "free", None, None
            if not u.blocked:
                sent += 1
                await send_to(u.tg_id, "⌛ 💎 Полный тариф закончился. Сейчас у вас бесплатный: заявок до 5 в день, "
                              "с задержкой. Продлить можно в любой момент.", button)
        await s.commit()
    return sent


async def expiry_loop() -> None:
    while True:
        try:
            await expiry_once()
        except Exception:
            log.exception("expiry_loop")
            await notify.alert_once("expiry_loop", "В фоновой задаче expiry_loop ошибка (см. логи сервера).")
        await asyncio.sleep(3600)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    if not config.BOT_TOKEN or not config.OWNER_IDS:
        raise SystemExit("Задайте BOT_TOKEN и OWNER_IDS в .env")
    await init_db()
    await upgrade_schema(engine)
    async with Session() as s:
        await seed(s)
        await migrate_data(s)
    manager = ScoutManager()
    bot, dp = build(manager)
    await manager.start()
    tasks = [
        asyncio.create_task(c)
        for c in (manager.watchdog(), join_loop(manager), search_loop(manager), rotation_loop(manager), reminder_loop(), delivery_loop(), channel_loop(), public_stats.stats_loop(bot),
          backup.backup_loop(), expiry_loop(), payments_h.link_loop(), cleanup_loop(), receipts_loop())
    ]
    await setup_profile(bot)
    await notify._send("🟢 Бот запущен (или перезапущен).")
    feed = await feed_server.start()
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        for t in tasks:
            t.cancel()
        if feed:
            await feed.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
