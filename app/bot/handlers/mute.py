"""🔕 Выключить уведомления о заявках на время (всем пользователям и админу). Логика — services/mute.py."""
import asyncio
import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message

from app.bot.common import kb, reply
from app.db import Session
from app.models import User, now
from app.services import mute

router = Router()
log = logging.getLogger(__name__)

SHOW_LIST = "ll:muted:0"


def _list_button(n: int):
    return [(f"📥 Накопилось без уведомлений · {n}", SHOW_LIST)]


async def menu(target: Message | CallbackQuery, user: User) -> None:
    async with Session() as s:
        row = await s.get(User, user.tg_id)
        n = await mute.accumulated(s, user.tg_id)
    if mute.is_muted(row):
        text = (f"🔕 Уведомления о заявках выключены {mute.until_text(row)}.\n"
                "Заявки не пропадают: они копятся в списке и ждут вас.")
        rows = [[("🔔 Включить уведомления сейчас", "mt:on")]]
        rows += [[("⏱ Изменить срок:", "noop")]]
    else:
        text = ("🔔 Уведомления о заявках включены.\n\n"
                "Можно выключить их на время: заявки продолжат приходить, но без сообщений — копятся в списке "
                "«📥 Накопилось без уведомлений». Когда срок кончится, уведомления включатся сами.\n\nНа сколько выключить?")
        rows = []
    opts = [(f"🔕 {label}", f"mt:set:{code}") for code, label in mute.OPTIONS]
    rows += [opts[i:i + 2] for i in range(0, len(opts), 2)]
    if n:
        rows.append(_list_button(n))
    await reply(target, text, kb(rows))


@router.callback_query(F.data == "mt:menu")
async def mt_menu(cb: CallbackQuery, user: User) -> None:
    await menu(cb, user)


@router.callback_query(F.data.regexp(r"^mt:set:(1h|3h|8h|morning|1d|3d|7d|off)$"))
async def mt_set(cb: CallbackQuery, user: User) -> None:
    async with Session() as s:
        await mute.set_mute(s, user, cb.data.split(":")[2])
        await s.commit()
        row = await s.get(User, user.tg_id)
    await cb.answer(f"🔕 Уведомления выключены {mute.until_text(row)}", show_alert=True)
    await menu(cb, user)


@router.callback_query(F.data == "mt:on")
async def mt_on(cb: CallbackQuery, user: User) -> None:
    async with Session() as s:
        n = await mute.unmute(s, user)
        await s.commit()
    await cb.answer("🔔 Уведомления снова включены")
    await reply(cb, "🔔 Уведомления о заявках снова включены." + (f"\nПока они были выключены, накопилось заявок: {n}." if n else ""),
                kb([_list_button(n)]) if n else None)


async def remind(message: Message, user: User) -> None:
    """При заходе в бота (любое сообщение): напомнить, что уведомления выключены. Не чаще раза в час."""
    async with Session() as s:
        row = await s.get(User, user.tg_id)
        if row is None or not mute.needs_reminder(row):
            return
        row.mute_noted_at = now()
        n = await mute.accumulated(s, user.tg_id)
        await s.commit()
    rows = [[("🔔 Включить", "mt:on"), ("⏱ Изменить срок", "mt:menu")]]
    if n:
        rows.insert(0, _list_button(n))
    try:
        await message.answer(f"🔕 Напоминаю: уведомления о заявках выключены {mute.until_text(row)}."
                             + (f" Накопилось заявок: {n}." if n else " Новых заявок пока нет."), reply_markup=kb(rows))
    except Exception:
        log.info("напоминание о выключенных уведомлениях не отправлено")


async def expire_loop(bot) -> None:
    """Срок вышел — уведомления включаются сами, человеку итог «накопилось N»."""
    while True:
        try:
            async with Session() as s:
                done = await mute.expired(s)
                counts = {u.tg_id: await mute.accumulated(s, u.tg_id) for u in done}
                await s.commit()
            for uid, n in counts.items():
                try:
                    await bot.send_message(uid, "🔔 Уведомления о заявках снова включены — срок вышел."
                                           + (f"\nПока они были выключены, накопилось заявок: {n}." if n else ""),
                                           reply_markup=kb([_list_button(n)]) if n else None)
                except Exception:
                    log.info("итог выключенных уведомлений не отправлен %s", uid)
        except Exception:
            log.exception("mute expire_loop")
        await asyncio.sleep(60)
