import asyncio
import logging

from sqlalchemy import select

from app import config
from app.bot import notify
from app.bot.handlers import mute as mute_h
from app.bot.handlers import payments as payments_h
from app.bot.handlers.stats import build_report
from app.bot.setup import build, setup_profile
from app.db import Session, engine, init_db
from app.migrate import migrate_data, upgrade_schema
from app.feed import server as feed_server
from app.web import server as web_server
from app.models import Client, Reminder, now
from app.services import backup, cleanup, heartbeat, public_stats, quiet, receipts, retention, settings
from app.scout.channels import channel_loop
from app.scout.joiner import join_loop, rotation_loop, search_loop
from app.scout.publisher import publish_loop
from app.scout.manager import ScoutManager
from app.seed import seed

log = logging.getLogger("crm")
REPEAT_TOUCH = "Вы ещё рассматриваете наше предложение? Могу ответить на вопросы."


async def send_to(user_id: int, text: str, markup=None) -> None:
    try:
        await notify._bot.send_message(user_id, text, reply_markup=markup, disable_web_page_preview=True)
    except Exception:
        log.info("не удалось написать %s", user_id)


async def reminders_once() -> int:
    """Разослать наступившие напоминания «дожать». Каждое — отдельно: одно сбойное не мешает остальным."""
    async with Session() as s:
        ids = (await s.scalars(select(Reminder.id).where(~Reminder.sent, Reminder.due_at <= now()))).all()
    sent = 0
    for rid in ids:
        async with Session() as s:
            r = await s.get(Reminder, rid)
            if r is None or r.sent:
                continue
            r.sent = True  # отмечаем сразу: даже если отправка не удастся, бот не будет повторять раз в минуту
            c = await s.get(Client, r.client_id)
            await s.commit()
        if c is None:
            continue
        who = c.name or c.username or c.tg_id
        await send_to(
            c.user_id or min(config.OWNER_IDS),
            f"⏰ {r.text or 'Напоминание'}: {who}\nМожно отправить: «{REPEAT_TOUCH}»",
            notify.InlineKeyboardMarkup(inline_keyboard=[[
                notify.InlineKeyboardButton(text="👤 Открыть", callback_data=f"cl:open:{c.id}")]]),
        )
        sent += 1
    return sent


async def reminder_loop() -> None:
    """Напоминания «дожать» (владельцу клиента) и вечерний отчёт администраторам."""
    last_report = None
    while True:
        try:
            await reminders_once()
        except Exception as e:
            log.exception("reminder_loop: напоминания")
            await notify.alert_once("reminder_loop", f"Напоминания клиентам: ошибка {type(e).__name__}: {str(e)[:150]}")
        try:
            t = quiet.local_now()
            async with Session() as s:
                report_hour = await settings.get_int(s, "report_hour")
            if t.hour == report_hour and last_report != t.date():
                last_report = t.date()  # даже при ошибке не повторяем отчёт каждую минуту
                await notify._send("🌙 Вечерний отчёт\n" + await build_report())
        except Exception as e:
            log.exception("reminder_loop: вечерний отчёт")
            await notify.alert_once("reminder_report", f"Вечерний отчёт: ошибка {type(e).__name__}: {str(e)[:150]}")
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


def local_day(moment) -> str:
    """Дата из базы (UTC) так, как её видит человек: по местному времени."""
    return quiet.to_local(moment).strftime("%d.%m")


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
        renew_on = payments.autorenew_on(await settings.get(s, "autorenew"))
        soon = (await s.scalars(select(User).where(
            User.plan == "full", User.plan_until.is_not(None), ~User.blocked,
            User.plan_until > now(), User.plan_until < now() + timedelta(days=3)))).all()
        for u in soon:
            if u.expiry_notice == u.plan_until:
                continue
            u.expiry_notice = u.plan_until
            sent += 1
            price = await payments.price_of(s, u.renew_days) if u.renew_method and renew_on else 0
            if price:
                text = (f"🔁 💎 Полный тариф заканчивается {local_day(u.plan_until)}: автоматически спишем {price} ₽ "
                        f"за продление на {u.renew_days} дн. Не нужно — отключите: 👤 Профиль → 💎 Тариф.")
                await send_to(u.tg_id, text)
            else:
                await send_to(u.tg_id, f"⏳ 💎 Полный тариф заканчивается {local_day(u.plan_until)}. "
                              "Продлите, чтобы заявки приходили сразу и без лимита.", button)
        ended = (await s.scalars(select(User).where(
            User.plan == "full", User.plan_until.is_not(None), User.plan_until <= now(), ~User.is_admin))).all()
        for u in ended:
            if renew_on and u.renew_method and u.renew_for != u.plan_until:
                continue  # автопродление ещё попробует — раньше времени «закончился» не пишем
            u.plan, u.plan_until, u.expiry_notice = "free", None, None
            u.winback_at = now() + timedelta(days=3)
            if not u.blocked:
                sent += 1
                await send_to(u.tg_id, "⌛ 💎 Полный тариф закончился. Сейчас у вас бесплатный: заявок до 5 в день, "
                              "с задержкой. Продлить можно в любой момент.", button)
        # через 3 дня после конца — одно мягкое «вернуть 💎», если человек так и не продлил
        back = (await s.scalars(select(User).where(
            User.plan == "free", User.winback_at.is_not(None), User.winback_at <= now(), ~User.blocked, ~User.is_admin))).all()
        offers = await payments.packages(s)
        for u in back:
            u.winback_at = None  # больше не напоминаем
            if not offers:
                continue
            days, price = offers[0]
            sent += 1
            await send_to(u.tg_id, "👋 Без 💎 заявки приходят с задержкой и не больше положенного в день — "
                          "другие специалисты успевают раньше. Вернуть полный тариф: "
                          f"{days} дн. — {price} ₽.", button)
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


async def _try_send(user_id: int, text: str, markup=None) -> bool:
    try:
        await notify._bot.send_message(user_id, text, reply_markup=markup, disable_web_page_preview=True)
        return True
    except Exception:
        return False


async def cleanup_loop() -> None:
    """Пустые аккаунты (не закончили регистрацию): сначала предупреждение, потом удаление."""
    while True:
        try:
            async with Session() as s:
                await cleanup.warn_due(s, _try_send)
            async with Session() as s:
                await cleanup.auto_cleanup(s)
        except Exception:
            log.exception("cleanup_loop")
            await notify.alert_once("cleanup_loop", "В фоновой задаче cleanup_loop ошибка (см. логи сервера).")
        await asyncio.sleep(6 * 3600)


async def retention_loop() -> None:
    """Раз в сутки удаляет заявки старше lead_keep_days (тексты и ники людей из групп не храним бессрочно)."""
    while True:
        try:
            async with Session() as s:
                days = await settings.get_int(s, "lead_keep_days")
            async with Session() as s:
                n = await retention.purge_old(s, days)
            if n:
                log.info("удалено старых заявок: %s (срок хранения %s дн.)", n, days)
        except Exception:
            log.exception("retention_loop")
            await notify.alert_once("retention_loop", "В фоновой задаче retention_loop ошибка (см. логи сервера).")
        await asyncio.sleep(24 * 3600)


async def receipts_loop() -> None:
    """Раз в месяц напоминает владельцу про чеки в «Мой налог»."""
    while True:
        try:
            async with Session() as s:
                text = await receipts.due_message(s)
            if text:
                await notify._send(text)
        except Exception:
            log.exception("receipts_loop")
            await notify.alert_once("receipts_loop", "В фоновой задаче receipts_loop ошибка (см. логи сервера).")
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
    from app.services import selfcheck

    loops = {
        "сторож разведчиков": manager.watchdog(), "вступление в группы": join_loop(manager), "публикация рекламы": publish_loop(manager),
        "поиск групп": search_loop(manager), "ротация групп": rotation_loop(manager), "напоминания": reminder_loop(),
        "раздача заявок": delivery_loop(), "открытые каналы": channel_loop(), "канал статистики": public_stats.stats_loop(bot),
        "копия базы": backup.backup_loop(), "конец тарифов": expiry_loop(), "оплата по ссылкам": payments_h.link_loop(),
        "чистка аккаунтов": cleanup_loop(), "срок хранения заявок": retention_loop(), "чеки": receipts_loop(),
        "сторож (heartbeat)": heartbeat.heartbeat_loop(),
        "включение уведомлений (🔕)": mute_h.expire_loop(bot),
    }
    selfcheck.TASKS = {name: asyncio.create_task(c) for name, c in loops.items()}  # для «🧪 Самопроверки»
    tasks = list(selfcheck.TASKS.values())
    await setup_profile(bot)
    await notify._send("🟢 Бот запущен (или перезапущен).")
    feed = await feed_server.start()
    webapp = await web_server.start()
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        for t in tasks:
            t.cancel()
        if feed:
            await feed.cleanup()
        if webapp:
            await webapp.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
