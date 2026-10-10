import asyncio
import logging
from datetime import timedelta

from sqlalchemy import select

from app import config
from app.bot import notify
from app.bot.handlers.stats import build_report
from app.bot.setup import build
from app.db import Session, init_db
from app.models import Client, Reminder, now
from app.scout.joiner import join_loop, rotation_loop, search_loop
from app.scout.manager import ScoutManager
from app.seed import seed

log = logging.getLogger("crm")
REPEAT_TOUCH = "Вы ещё рассматриваете наше предложение? Могу ответить на вопросы."


async def reminder_loop() -> None:
    """Напоминания «дожать» + вечерний отчёт (19:00 UTC ≈ 22:00 МСК)."""
    last_report = None
    while True:
        try:
            async with Session() as s:
                due = (await s.scalars(select(Reminder).where(~Reminder.sent, Reminder.due_at <= now()))).all()
                for r in due:
                    c = await s.get(Client, r.client_id)
                    r.sent = True
                    who = c.name or c.username or c.tg_id
                    await notify._send(
                        f"⏰ {r.text}: {who}\nМожно отправить: «{REPEAT_TOUCH}»",
                        notify.InlineKeyboardMarkup(inline_keyboard=[[
                            notify.InlineKeyboardButton(text="👤 Открыть", callback_data=f"cl:open:{c.id}")]]),
                    )
                await s.commit()
            t = now()
            if t.hour == 19 and last_report != t.date():
                last_report = t.date()
                await notify._send("🌙 Вечерний отчёт\n" + await build_report())
        except Exception:
            log.exception("reminder_loop")
        await asyncio.sleep(60)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    if not config.BOT_TOKEN or not config.OWNER_IDS:
        raise SystemExit("Задайте BOT_TOKEN и OWNER_IDS в .env")
    await init_db()
    async with Session() as s:
        await seed(s)
    manager = ScoutManager()
    bot, dp = build(manager)
    await manager.start()
    tasks = [
        asyncio.create_task(c)
        for c in (manager.watchdog(), join_loop(manager), search_loop(manager), rotation_loop(manager), reminder_loop())
    ]
    try:
        await dp.start_polling(bot)
    finally:
        for t in tasks:
            t.cancel()


if __name__ == "__main__":
    asyncio.run(main())
