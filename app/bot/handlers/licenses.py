"""🪪 Лицензии: список групп с лицензией, ручное продление и отключение."""
from aiogram import F, Router
from aiogram.types import CallbackQuery
from sqlalchemy import select

from app.bot.common import esc, kb, reply
from app.db import Session
from app.models import Group
from app.services import licenses, settings

router = Router()


async def licenses_menu(target) -> None:
    async with Session() as s:
        groups = (await s.scalars(select(Group).where(Group.catalog, Group.submitted_by != 0)
                                  .order_by(Group.license_until.desc(), Group.leads_count.desc()).limit(25))).all()
        on = await settings.get(s, "license_enabled") == "1"
    rows = [[(("✅ " if licenses.active(g) else "⛔ " if g.license_off else "⬜ ") + g.title[:30]
              + (f" · до {g.license_until:%d.%m}" if licenses.active(g) else ""), f"lic:open:{g.id}")] for g in groups]
    rows.append([("◀️ Назад", "bc:back")])
    head = ("🪪 <b>Лицензии бота модерации</b>\n" + ("Автовыдача за заявки: включена." if on else
            "Автовыдача выключена (⚙️ Настройки → 🌐 Каталог и каналы).") +
            "\n✅ действует · ⛔ отключена вручную · ⬜ нет\n"
            "Бот модерации узнаёт о лицензии запросом GET /feed/license?chat_id=…")
    await reply(target, head, kb(rows))


@router.callback_query(F.data == "lic:menu")
async def back(cb: CallbackQuery) -> None:
    await licenses_menu(cb)


async def _card(cb: CallbackQuery, gid: int) -> None:
    async with Session() as s:
        g = await s.get(Group, gid)
    if g is None:
        return await cb.answer("Группа не найдена", show_alert=True)
    state = "⛔ отключена вручную" if g.license_off else (f"✅ до {g.license_until:%d.%m.%Y}" if licenses.active(g) else "нет")
    await reply(cb, f"🪪 <b>{esc(g.title)}</b>\nЛицензия: {state}\nЗаявок из группы: {g.leads_count}", kb([
        [("➕ +30 дней", f"lic:add:{g.id}"), ("✅ Включить" if g.license_off else "⛔ Отключить", f"lic:tog:{g.id}")],
        [("◀️ К списку", "lic:menu")]]))


@router.callback_query(F.data.regexp(r"^lic:(open|add|tog):\d+$"))
async def act(cb: CallbackQuery) -> None:
    _, action, gid = cb.data.split(":")
    async with Session() as s:
        g = await s.get(Group, int(gid))
        if g is None:
            return await cb.answer("Группа не найдена", show_alert=True)
        if action == "add":
            await licenses.grant(s, g, 30)
        elif action == "tog":
            await licenses.toggle(s, g)
    await cb.answer()
    await _card(cb, int(gid))
