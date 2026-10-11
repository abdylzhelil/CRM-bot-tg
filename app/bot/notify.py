"""Отправка владельцу: карточки заявок, карточки групп, алерты."""
import html
import logging

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app import config
from app.db import Session
from app.models import Group, Lead
from app.services import quiet, settings

log = logging.getLogger(__name__)
_bot: Bot | None = None


def init(bot: Bot) -> None:
    global _bot
    _bot = bot


async def _send(text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    assert _bot, "notify.init(bot) не вызван"
    for uid in config.OWNER_IDS:
        try:
            await _bot.send_message(uid, text, reply_markup=markup, disable_web_page_preview=True)
        except Exception:
            log.exception("не удалось отправить сообщение %s", uid)


async def alert(text: str) -> None:
    await _send(f"🚨 {html.escape(text)}")


def lead_keyboard(lead: Lead) -> InlineKeyboardMarkup:
    if lead.username:
        reply = InlineKeyboardButton(text="✍️ Ответить", url=f"https://t.me/{lead.username}")
    else:
        reply = InlineKeyboardButton(text="✍️ Ответить", url=f"tg://user?id={lead.author_id}")
    rows = [
        [reply, InlineKeyboardButton(text="📋 Шаблон", callback_data=f"lead:tpl:{lead.id}")],
        [InlineKeyboardButton(text="✅ В работу", callback_data=f"lead:take:{lead.id}")],
        [
            InlineKeyboardButton(text="⏰ Отложить", callback_data=f"lead:later:{lead.id}"),
            InlineKeyboardButton(text="❌ Отказ", callback_data=f"lead:reject:{lead.id}"),
            InlineKeyboardButton(text="🚫 Спам", callback_data=f"lead:spam:{lead.id}"),
        ],
    ]
    if lead.suspicious:
        rows.append([InlineKeyboardButton(text="👍 Это клиент", callback_data=f"lead:client:{lead.id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def lead_text(lead: Lead, category: str) -> str:
    if lead.suspicious:
        head = "⚠️ Подозрительный"
    else:
        head = "🔥 Горячий клиент" if quiet.is_hot(lead.text) else "🆕 Новый клиент"
    who = html.escape(lead.name or "—")
    if lead.username:
        who += f" (@{html.escape(lead.username)})"
    text = html.escape(lead.text.strip())
    if len(text) > 400:
        text = text[:400] + "…"
    link = f'\n<a href="{lead.link}">Ссылка на сообщение</a>' if lead.link else ""
    spam = f"\nСпам-скор: {lead.spam_score}" if lead.suspicious else ""
    return (
        f"{head} | {html.escape(category)}\nКто: {who}\nГруппа: {html.escape(lead.group_title)}\n"
        f"Суть: {text}{link}{spam}"
    )


async def send_lead(lead: Lead, category: str) -> None:
    await _send(lead_text(lead, category), lead_keyboard(lead))
    async with Session() as s:
        row = await s.get(Lead, lead.id)
        if row:
            row.notified = True
            await s.commit()


async def deliver_lead(lead: Lead, category: str) -> None:
    """В режим тишины обычные заявки копятся (их пришлёт quiet_digest), 🔥 горячие идут сразу."""
    async with Session() as s:
        hours = quiet.parse_hours(await settings.get(s, "quiet_hours"))
    if quiet.in_quiet(quiet.local_now().hour, hours) and not quiet.is_hot(lead.text):
        return
    await send_lead(lead, category)


def group_text(g: Group) -> str:
    uname = f"@{g.username}" if g.username else "без username"
    act = "н/д" if g.msgs_per_day < 0 else f"{g.msgs_per_day:.0f}"
    return (
        f"🔍 Найдена группа\n<b>{html.escape(g.title)}</b> ({html.escape(uname)})\n"
        f"Участников: {g.members} · сообщений/сутки: {act}"
    )


async def send_group_card(g: Group) -> None:
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="✅ Вступить", callback_data=f"grp:join:{g.id}"),
            InlineKeyboardButton(text="❌ Пропустить", callback_data=f"grp:skip:{g.id}"),
        ]]
    )
    await _send(group_text(g), kb)
