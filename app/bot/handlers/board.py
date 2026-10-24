"""📊 Рейтинг заявок: в закрепе групп с ботом (services/boards.py), в боте по кнопке и у админа в 🔍 Группы."""
import asyncio
import html
import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select

from app.bot.common import esc, kb, reply
from app.db import Session
from app.models import Group, User, now
from app.services import boards, settings

log = logging.getLogger(__name__)
router = Router()  # личка: рейтинг для пользователей
admin_router = Router()  # 🔍 Группы → 📊 Рейтинг в группах
group_router = Router()  # в группах: /rating (раньше общего «читателя» сообщений ingroup)


async def _can_pin(bot: Bot, chat_id: int) -> tuple[bool, str]:
    me = await bot.me()
    try:
        m = await bot.get_chat_member(chat_id, me.id)
    except Exception as e:
        return False, f"не вижу свои права ({type(e).__name__})"
    if m.status == "creator":
        return True, ""
    if m.status != "administrator":
        return False, "бот не админ группы — рейтинг не публикуется"
    if not getattr(m, "can_pin_messages", False):
        return False, "бот админ, но без права закреплять сообщения"
    return True, ""


async def update_one(bot: Bot, gid: int) -> str:
    """Выложить/обновить рейтинг в группе. Возвращает пояснение ("" — всё хорошо)."""
    async with Session() as s:
        g = await s.get(Group, gid)
        if g is None or not g.bot_member or not g.board_on or await settings.get(s, "board_enabled") != "1":
            return "рейтинг выключен"
        body = await boards.group_text(s, g)
        chat_id, msg_id = g.tg_id, g.board_msg_id
        await s.commit()  # не держим базу, пока ждём Telegram
    ok, note = await _can_pin(bot, chat_id)
    new_id = msg_id
    if ok:
        me = await bot.me()
        markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
            text="📥 Получать заявки", url=boards.join_link(me.username, gid))]])
        sent = False
        if msg_id:
            try:
                await bot.edit_message_text(body, chat_id=chat_id, message_id=msg_id, reply_markup=markup,
                                            disable_web_page_preview=True)
                sent = True
            except Exception as e:
                sent = "not modified" in str(e)  # то же самое — оставляем как есть
        if not sent:
            try:
                msg = await bot.send_message(chat_id, body, reply_markup=markup, disable_notification=True,
                                             disable_web_page_preview=True)
                new_id = msg.message_id
                try:
                    await bot.pin_chat_message(chat_id, new_id, disable_notification=True)
                except Exception:
                    note = "выложил, но не смог закрепить"
            except Exception as e:
                ok, note = False, f"не смог написать в группу ({type(e).__name__})"
    async with Session() as s:
        g = await s.get(Group, gid)
        if g is not None:
            g.board_at, g.board_note, g.board_msg_id = now(), note[:120], new_id
            await s.commit()
    return note


async def board_loop(bot: Bot) -> None:
    """Раз в 10 минут: обновить рейтинг там, где подошёл срок; раз в день — готовые предложения админам."""
    from app.services import botoffer

    while True:
        try:
            async with Session() as s:
                ids = [g.id for g in await boards.due(s)]
            for gid in ids:
                await update_one(bot, gid)
                await asyncio.sleep(2)
            await botoffer.daily(bot)
        except Exception:
            log.exception("board_loop")
        await asyncio.sleep(600)


# ---------- в группе: /rating on|off — только админы группы ----------
@group_router.message(Command("rating"), F.chat.type.in_({"group", "supergroup"}))
async def rating_cmd(m: Message, bot: Bot) -> None:
    try:
        member = await bot.get_chat_member(m.chat.id, m.from_user.id)
    except Exception:
        return
    if member.status not in ("administrator", "creator"):
        return
    arg = (m.text or "").split()[1:2]
    async with Session() as s:
        g = await s.scalar(select(Group).where(Group.tg_id == m.chat.id))
        if g is None:
            return
        if arg and arg[0].lower() in ("off", "выкл"):
            g.board_on = False
        elif arg and arg[0].lower() in ("on", "вкл"):
            g.board_on, g.board_at = True, None
        on, gid = g.board_on, g.id
        await s.commit()
    if on:
        note = await update_one(bot, gid)
        await m.reply("📊 Рейтинг заявок включён" + (f" — но {note}" if note else "") + ". Выключить: /rating off")
    else:
        await m.reply("📊 Рейтинг заявок выключен. Включить: /rating on")


# ---------- в боте: рейтинг по нишам города пользователя; заявки ниши — на 💎 ----------
async def add_link() -> str:
    """Одна кнопка: добавить бота в свою группу СРАЗУ админом с правом закреплять (Telegram сам спросит права)."""
    try:
        from app.bot import notify

        me = await notify._bot.me()
        return f"https://t.me/{me.username}?startgroup=rating&admin=pin_messages"
    except Exception:
        return ""


def group_of(payload: str | None) -> int:
    """`src_g12` (кнопка «📥 Получать заявки» в закрепе группы) → 12, иначе 0."""
    import re

    m = re.fullmatch(r"src_g(\d+)", payload or "")
    return int(m.group(1)) if m else 0


def _user_place(user: User) -> tuple[str, str, str]:
    from app.services import geo

    city = user.city or ""
    region = geo.region_of(city)
    if city in geo.REGIONS and city not in geo.CITIES:
        city, region = "", city
    return city, region, user.city or "вся Россия"


async def rating_view(target, user: User, group_id: int = 0) -> None:
    """🏆 Рейтинг: кнопка в главном меню, «📥 Получать заявки» из группы (место — как у группы), /start rating."""
    async with Session() as s:
        g = await s.get(Group, group_id) if group_id else None
        city, region, label = boards.place_of(g) if g is not None else _user_place(user)
        body = await boards.text(s, city, region, label)
        items, _, _ = await boards.counts(s, city, region)
        mine = await boards.my_groups(s, user.tg_id)
    if mine:
        body += "\n\n🤝 <b>Группы, куда вы добавили бота</b>:\n" + "\n".join(
            f"• {esc(g.title)} — пришло {came}, с 💎 {paid}, вам начислено {g.board_earned:.1f} дн."
            + (f" ({esc(g.board_note)})" if g.board_note else "") for g, came, paid in mine)
    suffix = f":{group_id}" if group_id else ""
    rows = [[(f"🔍 {name}"[:40], f"rt:n:{cid}{suffix}")] for cid, name, _, _ in items[:8]]
    link = await add_link()
    if link:
        rows.append([("➕ Поставить бота в мою группу", f"url:{link}")])
        body += ("\n\n➕ Есть своя группа или чат? Поставьте бота — в закрепе будет такой рейтинг, а вам — "
                 "дни 💎 с оплат участников.")
    if not user.onboarded:
        body += "\n\nЧтобы получать такие заявки себе — выберите свои ниши ниже 👇"
    else:
        rows.append([("◀️ Закрыть", "rt:close")])
    await reply(target, body + "\n\nНажмите нишу, чтобы увидеть сами заявки (на 💎).", kb(rows))


@router.callback_query(F.data == "rt:menu")
async def rating_menu(cb: CallbackQuery, user: User) -> None:
    await rating_view(cb, user)


@router.callback_query(F.data.regexp(r"^rt:n:\d+(:\d+)?$"))
async def rating_niche(cb: CallbackQuery, user: User) -> None:
    parts = cb.data.split(":")
    cid, gid = int(parts[2]), int(parts[3]) if len(parts) > 3 else 0
    back = f"rt:g:{gid}" if gid else "rt:menu"
    if user.plan != "full" and not user.is_admin:
        return await reply(cb, "🔒 Сами заявки по всем нишам и подробности — на полном тарифе 💎.\n"
                               "Бесплатно приходят заявки по вашим нишам (до 5 в день).",
                           kb([[("💎 Тариф", "pf:plan")], [("◀️ Назад", back)]]))
    async with Session() as s:
        g = await s.get(Group, gid) if gid else None
        city, region, label = boards.place_of(g) if g is not None else _user_place(user)
        leads = await boards.leads_of(s, cid, city, region)
    if not leads:
        return await cb.answer("За неделю заявок в этой нише нет", show_alert=True)
    lines = [f"🔍 Последние заявки ниши ({esc(label)}):"]
    for ld in leads:
        lines.append(f"\n• {ld.created_at:%d.%m} · {esc(ld.city or ld.region or '')} · {esc(ld.group_title[:30])}\n"
                     f"{html.escape(' '.join(ld.text.split())[:220])}")
    await reply(cb, "\n".join(lines)[:3900], kb([[("◀️ Назад", back)]]))


@router.callback_query(F.data.regexp(r"^rt:g:\d+$"))
async def rating_group(cb: CallbackQuery, user: User) -> None:
    await rating_view(cb, user, group_id=int(cb.data.split(":")[2]))


@router.callback_query(F.data == "rt:close")
async def rating_close(cb: CallbackQuery) -> None:
    try:
        await cb.message.delete()
    except Exception:
        pass
    await cb.answer()


# ---------- админ: 🔍 Группы → 📊 Рейтинг в группах ----------
@admin_router.callback_query(F.data == "gr:brd")
async def admin_boards(cb: CallbackQuery) -> None:
    async with Session() as s:
        groups_ = (await s.scalars(select(Group).where(Group.bot_member, Group.status == "joined")
                                   .order_by(Group.members.desc()).limit(20))).all()
        on = await settings.get(s, "board_enabled") == "1"
        pct = await settings.get_int(s, "board_percent")
        stats = {}
        for g in groups_:
            admin = await s.get(User, g.board_admin) if g.board_admin else None
            came = await s.scalar(select(func.count()).select_from(User).where(
                User.source == boards.source_tag(g.id))) or 0
            stats[g.id] = (admin, came)
    head = ("📊 <b>Рейтинг в группах</b> — в группах, где КлиентРадар админ с правом закреплять, в закрепе висит рейтинг "
            f"заявок по нишам и кнопка «📥 Получать заявки». Админу, который добавил бота, — {pct}% купленных дней 💎 "
            "с каждой оплаты пришедших. Админ группы выключает командой /rating off."
            + ("" if on else "\n⏸ Выключено: ⚙️ → 📊 Рейтинг в группах."))
    if not groups_:
        return await reply(cb, head + "\n\nБота пока нет ни в одной группе — «➕ Добавить бота в группу».",
                           kb([[("◀️ Назад", "gr:menu")]]))
    await reply(cb, head, kb([[("◀️ Назад", "gr:menu")]]))
    for g in groups_:
        admin, came = stats[g.id]
        text = (f"<b>{esc(g.title)}</b> · {g.members} уч.\n"
                + ("📌 в закрепе" if g.board_msg_id and not g.board_note else f"⚠️ {esc(g.board_note or 'ещё не публиковался')}")
                + ("" if g.board_on else " · ⏸ выключен")
                + f"\nДобавил: {esc(admin.name) if admin else ('id ' + str(g.board_admin) if g.board_admin else '—')}"
                + f" · пришло людей: {came} · начислено: {g.board_earned:.1f} дн.")
        await cb.message.answer(text, reply_markup=kb([[("🔄 Обновить", f"gr:bdu:{g.id}"),
                                                         ("⏸ Выключить" if g.board_on else "▶️ Включить", f"gr:bdt:{g.id}")]]))


@admin_router.callback_query(F.data.regexp(r"^gr:bd[ut]:\d+$"))
async def admin_board_action(cb: CallbackQuery, bot: Bot) -> None:
    op, gid = cb.data.split(":")[1], int(cb.data.split(":")[2])
    async with Session() as s:
        g = await s.get(Group, gid)
        if g is None:
            return await cb.answer("Группа не найдена", show_alert=True)
        if op == "bdt":
            g.board_on = not g.board_on
        g.board_at = None
        on = g.board_on
        await s.commit()
    if op == "bdt" and not on:
        return await cb.answer("Рейтинг в этой группе выключен (старое сообщение можно удалить в группе вручную)",
                               show_alert=True)
    note = await update_one(bot, gid)
    await cb.answer("✅ Обновил" if not note else f"⚠️ {note}", show_alert=bool(note))
