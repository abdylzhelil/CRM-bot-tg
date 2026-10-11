import asyncio
import logging

from sqlalchemy import select

from app import config
from app.bot import notify
from app.bot.handlers.stats import build_report
from app.bot.setup import build
from app.db import Session, engine, init_db
from app.migrate import migrate_data, upgrade_schema
from app.feed import server as feed_server
from app.models import Client, Reminder, now
from app.services import quiet, settings
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
        await asyncio.sleep(60)


async def delivery_loop() -> None:
    """Заявки с задержкой (бесплатный тариф) и накопленные за режим тишины."""
    while True:
        try:
            await notify.flush_due()
        except Exception:
            log.exception("delivery_loop")
        await asyncio.sleep(20)


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
        for c in (manager.watchdog(), join_loop(manager), search_loop(manager), rotation_loop(manager), reminder_loop(), delivery_loop())
    ]
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
