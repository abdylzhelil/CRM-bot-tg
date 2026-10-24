"""Отправка владельцу: карточки заявок, карточки групп, алерты."""
import asyncio
import html
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramNetworkError, TelegramRetryAfter, TelegramServerError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app import config
from app.db import Session
from app.models import Delivery, Group, Lead, User
from app.services import mute, quiet

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

    await _log_error(key, text)
    if time.monotonic() - _last_alert.get(key, -cooldown - 1) < cooldown:
        return
    _last_alert[key] = time.monotonic()
    try:
        await alert(text)
    except Exception:
        log.exception("не удалось отправить тревогу")


async def _log_error(key: str, text: str) -> None:
    """Записать сбой в журнал (для «🩺 Отчёт о проблемах»); сам журнал никогда не роняет бота."""
    from app.models import ErrorLog

    try:
        async with Session() as s:
            s.add(ErrorLog(key=key[:64], text=text[:500]))
            await s.commit()
    except Exception:
        log.warning("журнал ошибок недоступен")


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
    ]
    if lead.link and not getattr(lead, "gone_at", None):  # бот сам не пишет клиенту — отвечаем на его сообщение в группе
        rows.append([InlineKeyboardButton(text="💬 Ответить в группе", url=lead.link)])
    rows += [
        [InlineKeyboardButton(text="✅ В работу", callback_data=f"lead:take:{d}"),
         InlineKeyboardButton(text="📨 Коллеге", callback_data=f"lead:pass:{d}")],
        [
            InlineKeyboardButton(text="⏰ Отложить", callback_data=f"lead:later:{d}"),
            InlineKeyboardButton(text="🙈 Не моё", callback_data=f"lead:reject:{d}"),
            InlineKeyboardButton(text="🚫 Спам", callback_data=f"lead:spam:{d}"),
        ],
    ]
    rows.append([InlineKeyboardButton(text="✨ Черновик ответа", callback_data=f"lead:ai:{d}"),
                 InlineKeyboardButton(text="🔕 Не беспокоить", callback_data="mt:menu")])
    # все учат фильтр: «это реклама услуг» (у админа — сразу, у остальных — после нескольких голосов)
    extra = [InlineKeyboardButton(text="🙅 Исполнитель", callback_data=f"lead:offer:{d}")]
    if admin and lead.suspicious:
        extra.insert(0, InlineKeyboardButton(text="👍 Это клиент", callback_data=f"lead:client:{d}"))
    rows.append(extra)
    if admin and lead.group_id:  # группа-свалка рекламы/флуда: выключить заявки из неё целиком
        rows.append([InlineKeyboardButton(text="🗑 Не брать из этой группы", callback_data=f"lead:nogrp:{d}")])
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
    if getattr(lead, "gone_at", None):
        link = "\n🗑 Сообщение уже удалено в группе — отвечайте автору в личку"
    place = f"\nГде: {html.escape(lead.city or lead.region)}" if (lead.city or lead.region) else ""
    spam = f"\nСпам-скор: {lead.spam_score}" if lead.suspicious else ""
    return (
        f"{head} | {html.escape(category)}\nКто: {who}\nГруппа: {html.escape(lead.group_title)}\n"
        f"Суть: {text}{place}{link}{spam}"
    )


SENT, BLOCKED, RETRY = "sent", "blocked", "retry"
_flush_lock = asyncio.Lock()


async def send_delivery(d: Delivery, lead: Lead, user: User, category: str) -> str:
    """Карточка заявки пользователю: SENT; BLOCKED — заблокировал бота; RETRY — Telegram временно не принял, повторим."""
    assert _bot, "notify.init(bot) не вызван"
    text = lead_text(lead, category)
    if user.is_admin and getattr(lead, "matched", ""):  # админу: по каким словам ниши заявка пришла
        text += f"\n🔑 Слова: {html.escape(lead.matched)}"
    if d.from_user:
        async with Session() as s:
            sender = await s.get(User, d.from_user)
        text = f"📨 Передал(а): {html.escape((sender.name if sender else '') or 'коллега')}\n" + text
    try:
        await _bot.send_message(
            user.tg_id, text, reply_markup=lead_keyboard(lead, d.id, user.is_admin),
            disable_web_page_preview=True,
        )
        return SENT
    except TelegramForbiddenError:
        return BLOCKED
    except (TelegramRetryAfter, TelegramNetworkError, TelegramServerError):
        log.warning("Telegram временно не принял заявку %s для %s — повторим", lead.id, user.tg_id)
        return RETRY
    except Exception:
        log.exception("не удалось отправить заявку %s пользователю %s", lead.id, user.tg_id)
        return SENT  # ошибка в самом сообщении — не повторяем бесконечно


async def flush_due() -> int:
    """Отправить все заявки, время которых пришло. Возвращает, сколько отправлено.

    Вызывается из нескольких мест сразу (новая заявка, передача коллеге, фоновая задача), поэтому идёт по очереди
    (замок), и каждая отметка «отправлено» сохраняется сразу — одна заявка не уйдёт человеку дважды.
    """
    from app.bot.handlers.leads import category_name
    from app.services.distribute import due

    sent = 0
    async with _flush_lock:
        async with Session() as s:
            for d, lead, user in await due(s):
                if mute.is_muted(user):  # 🔕 уведомления выключены: заявка ложится в список без сообщения
                    d.sent, d.muted = True, True
                    await s.commit()
                    continue
                category = await category_name(s, lead.category_id)
                result = await send_delivery(d, lead, user, category)
                if result == RETRY:
                    break  # Telegram просит подождать — остальное отправит следующий проход
                d.sent = True
                if result == BLOCKED:
                    user.blocked = True
                sent += result == SENT
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


async def user_search_result(user_id: int, query: str, c: dict) -> None:
    """Итог поиска «🔎 Найти группы для меня» — самому пользователю."""
    if not _bot:
        return
    q = html.escape(query)
    if c["found"]:
        text = (f"🔎 Поиск групп «{q}»: нашли новых — {c['found']}.\n"
                "Разведчик проверит их и вступит в ближайшие дни. Заявки оттуда по вашим нишам вы будете получать в первую очередь.")
    else:
        text = (f"🔎 Поиск групп «{q}»: новых групп не нашли.\n"
                "Попробуйте другие слова: услуга + город («ремонт Подольск», «торты Казань») или «барахолка Ваш город».")
    extra = []
    if c.get("known"):
        extra.append(f"уже знали {c['known']} (их и так читаем)")
    if c.get("junked") or c.get("small"):
        extra.append(f"не подошли {c.get('junked', 0) + c.get('small', 0)} (реклама, флуд, мелкие или тихие)")
    if extra:
        text += "\n" + "; ".join(extra).capitalize() + "."
    try:
        await _bot.send_message(user_id, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔎 Мои поиски групп", callback_data="ms:list")]]))
    except Exception:
        log.info("итог поиска групп не доставлен %s", user_id)


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
