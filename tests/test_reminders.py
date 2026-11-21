"""Напоминания «дожать»: каждое отдельно, сбойное (клиент удалён, человек заблокировал бота) не стопорит остальные."""
from datetime import timedelta

from sqlalchemy import select

from app.models import Client, Group, Reminder, now
from tests.conftest import make_user


async def test_reminders_sent_once_and_bad_ones_do_not_block(maker, tgbot):
    from app import main

    await make_user(maker, 90)
    await make_user(maker, 91)
    tgbot.blocked.add(91)  # заблокировал бота — отправка упадёт
    async with maker() as s:
        c1 = Client(user_id=90, name="Иван")
        c2 = Client(user_id=91, name="Пётр")
        s.add_all([c1, c2])
        await s.flush()
        s.add_all([Reminder(client_id=c2.id, due_at=now() - timedelta(minutes=5), text="Дожать"),
                   Reminder(client_id=c1.id, due_at=now() - timedelta(minutes=3), text="Клиент думает — дожать"),
                   Reminder(client_id=c1.id, due_at=now() + timedelta(days=1), text="Потом")])
        await s.commit()
    await main.reminders_once()
    assert any("Иван" in t for t in tgbot.texts(90))
    async with maker() as s:
        left = (await s.scalars(select(Reminder).where(~Reminder.sent))).all()
    assert [r.text for r in left] == ["Потом"]  # остальные отмечены и не повторяются
    await main.reminders_once()
    assert len(tgbot.texts(90)) == 1


async def test_long_strings_are_cut_on_update(maker):
    async with maker() as s:
        g = Group(tg_id=-55, title="коротко")
        s.add(g)
        await s.commit()
        g.title = "я" * 400  # String(255)
        await s.commit()
        assert len((await s.get(Group, g.id)).title) == 255


async def test_evening_report_builds(maker):
    from app.bot.handlers.stats import build_report

    async with maker() as s:
        s.add_all([Client(user_id=1, name=f"c{i}", stage=st, budget=1000) for i, st in
                   enumerate(["new", "talk", "thinking", "payment", "done", "lost"])])
        await s.commit()
    text = await build_report()
    assert "Сумма выполненных: 1000" in text
