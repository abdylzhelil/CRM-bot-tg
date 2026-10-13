"""Отправка владельцу: карточки заявок, карточки групп, алерты."""
import html
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app import config
from app.db import Session
from app.models import Delivery, Group, Lead, User
from app.services import quiet

log = logging.getLogger(__name__)
_bot: Bot | None = None


def init(bot: Bot) -> None:
    global _bot
    _bot = bot


async def _send(text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    """Служебное сообщение администраторам (владельцам из OWNER_IDS)."""
    assert _bot, "notify.init(bot) не вызван"
    for uid in config.OWNER_IDS:
        try:
            await _bot.send_message(uid, text, reply_markup=markup, disable_web_page_preview=True)
        except Exception:
            log.exception("не удалось отправить сообщение %s", uid)


_last_alert: dict[str, float] = {}


async def alert_once(key: str, text: str, cooldown: float = 900) -> None:
    """Тревога администраторам, не чаще раза в cooldown секунд на один ключ (чтобы не засыпать сообщениями)."""
    import time

    if time.monotonic() - _last_alert.get(key, -cooldown - 1) < cooldown:
        return
    _last_alert[key] = time.monotonic()
    try:
        await alert(text)
    except Exception:
        log.exception("не удалось отправить тревогу")


async def alert(text: str) -> None:
    await _send(f"🚨 {html.escape(text)}")


def lead_keyboard(lead: Lead, delivery_id: int, admin: bool = False) -> InlineKeyboardMarkup:
    if lead.username:
        reply = InlineKeyboardButton(text="✍️ Ответить", url=f"https://t.me/{lead.username}")
    else:
        reply = InlineKeyboardButton(text="✍️ Ответить", url=f"tg://user?id={lead.author_id}")
    d = delivery_id
    rows = [
        [reply, InlineKeyboardButton(text="📋 Шаблон", callback_data=f"lead:tpl:{d}")],
        [InlineKeyboardButton(text="✅ В работу", callback_data=f"lead:take:{d}"),
         InlineKeyboardButton(text="📨 Коллеге", callback_data=f"lead:pass:{d}")],
        [
            InlineKeyboardButton(text="⏰ Отложить", callback_data=f"lead:later:{d}"),
            InlineKeyboardButton(text="❌ Отказ", callback_data=f"lead:reject:{d}"),
            InlineKeyboardButton(text="🚫 Спам", callback_data=f"lead:spam:{d}"),
        ],
    ]
    if lead.suspicious and admin:
        rows.append([InlineKeyboardButton(text="👍 Это клиент", callback_data=f"lead:client:{d}")])
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
    place = f"\nГде: {html.escape(lead.city or lead.region)}" if (lead.city or lead.region) else ""
    spam = f"\nСпам-скор: {lead.spam_score}" if lead.suspicious else ""
    return (
        f"{head} | {html.escape(category)}\nКто: {who}\nГруппа: {html.escape(lead.group_title)}\n"
        f"Суть: {text}{place}{link}{spam}"
    )


async def send_delivery(d: Delivery, lead: Lead, user: User, category: str) -> bool:
    """Карточка заявки пользователю. False — пользователь недоступен (заблокировал бота)."""
    assert _bot, "notify.init(bot) не вызван"
    text = lead_text(lead, category)
    if d.from_user:
        async with Session() as s:
            sender = await s.get(User, d.from_user)
        text = f"📨 Передал(а): {html.escape((sender.name if sender else '') or 'коллега')}\n" + text
    try:
        await _bot.send_message(
            user.tg_id, text, reply_markup=lead_keyboard(lead, d.id, user.is_admin),
            disable_web_page_preview=True,
        )
        return True
    except TelegramForbiddenError:
        return False
    except Exception:
        log.exception("не удалось отправить заявку %s пользователю %s", lead.id, user.tg_id)
        return True  # не повторяем бесконечно


async def flush_due() -> int:
    """Отправить все заявки, время которых пришло. Возвращает, сколько отправлено."""
    from app.bot.handlers.leads import category_name
    from app.services.distribute import due

    sent = 0
    async with Session() as s:
        for d, lead, user in await due(s):
            ok = await send_delivery(d, lead, user, await category_name(s, lead.category_id))
            d.sent = True
            if not ok:
                user.blocked = True
            sent += ok
        await s.commit()
    return sent


def group_text(g: Group) -> str:
    uname = f"@{g.username}" if g.username else "без username"
    act = "н/д" if g.msgs_per_day < 0 else f"{g.msgs_per_day:.0f}"
    return (
        f"🔍 Найдена группа\n<b>{html.escape(g.title)}</b> ({html.escape(uname)})\n"
        f"Участников: {g.members} · сообщений/сутки: {act}"
    )


async def groups_found(count: int, auto: bool) -> None:
    if auto:
        text = f"🔍 Поиск: найдено новых групп — {count}, они в очереди на вступление."
    else:
        text = f"🔍 Поиск: найдено новых групп — {count}. Отберите, в какие вступать:"
    await _send(text, InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="❓ Отобрать группы", callback_data="rv:page")]]))


async def send_group_card(g: Group) -> None:
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="✅ Вступить", callback_data=f"grp:join:{g.id}"),
            InlineKeyboardButton(text="❌ Пропустить", callback_data=f"grp:skip:{g.id}"),
        ]]
    )
    await _send(group_text(g), kb)


def _span(delta) -> str:
    hours = round(delta.total_seconds() / 3600)
    return f"{hours // 24} дн." if hours % 24 == 0 else f"{hours} ч."


async def bonus(user_id: int, delta, kind: str = "bonus") -> None:
    """Бонус автору группы или его пригласившему."""
    assert _bot, "notify.init(bot) не вызван"
    text = (
        f"🎁 Заявки из добавленной вами группы приносят пользу — дарим +{_span(delta)} 💎 Полного тарифа. Спасибо!"
        if kind == "bonus" else
        f"🤝 Приглашённый вами пользователь получил бонус — вам +{_span(delta)} 💎 Полного тарифа."
    )
    try:
        await _bot.send_message(user_id, text)
    except Exception:
        log.info("не удалось сообщить о бонусе %s", user_id)


async def level_up(user_id: int, level: int) -> None:
    from app.services import levels

    assert _bot, "notify.init(bot) не вызван"
    try:
        await _bot.send_message(user_id, f"🏆 Новый уровень: {levels.name(level)}! Группы, которые вы добавили, работают на вас.")
    except Exception:
        log.info("не удалось сообщить об уровне %s", user_id)
