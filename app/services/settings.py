"""Настройки в БД (key-value), редактируются из бота."""
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Setting

DEFAULTS: dict[str, str] = {
    "mode": "semi",  # semi — спрашивать о вступлении, auto — вступать самому
    "min_members": "300",
    "min_msgs_per_day": "10",
    "default_join_limit": "10",
    "search_interval_h": "6",
    "spam_suspicious": "30",
    "spam_block": "60",
    "dedupe_days": "7",
    "rotation_days": "30",
    "quiet_hours": "",
    "report_hour": "21",
    "max_recipients": "5",
    "free_daily_limit": "5",
    "free_delay_min": "30",
    "spam_votes_to_ban": "3",
    "scout_paused": "0",
    "submit_daily_limit": "3",
    "catalog_min_members": "50",
    "bonus_leads": "5",
    "channel_poll_min": "15",
    "ref_days": "7",
    "ref_percent": "20",
    "pass_daily_limit": "10",
    "share_gap_min": "2",
    "price_30": "0",
    "price_90": "0",
    "price_365": "0",
    "channel_hour": "12",
    "niche_daily_limit": "3",
    "ref_limit": "10",
    "bonus_max_days": "180",
}

LABELS: dict[str, str] = {
    "mode": "Режим вступления (semi/auto)",
    "min_members": "Мин. участников в группе",
    "min_msgs_per_day": "Мин. сообщений в сутки",
    "default_join_limit": "Вступлений в сутки на новый аккаунт",
    "search_interval_h": "Поиск групп раз в N часов",
    "spam_suspicious": "Порог «подозрительный» (0–100)",
    "spam_block": "Порог «спам» (0–100)",
    "dedupe_days": "Окно антидублей, дней",
    "rotation_days": "Выходить из групп без заявок за N дней",
    "report_hour": "Час вечернего отчёта",
    "max_recipients": "Сколько пользователей получают одну заявку",
    "free_daily_limit": "Бесплатный тариф: заявок в день",
    "free_delay_min": "Бесплатный тариф: задержка, мин",
    "spam_votes_to_ban": "Голосов «спам» для общего чёрного списка",
    "submit_daily_limit": "Групп от одного пользователя в сутки",
    "catalog_min_members": "Каталог: мин. участников в добавленной группе",
    "bonus_leads": "Заявок из группы пользователя за +1 день 💎",
    "channel_poll_min": "Опрос открытых каналов раз в N минут",
    "ref_days": "Приглашение: дней 💎 обоим",
    "ref_percent": "Приглашение: % от бонусных дней приглашённого",
    "pass_daily_limit": "Передач заявок коллегам в сутки",
    "share_gap_min": "Одну заявку получают несколько: интервал между ними, мин",
    "price_30": "Цена 💎 на 30 дней, ₽ (0 — не продавать)",
    "price_90": "Цена 💎 на 90 дней, ₽ (0 — не продавать)",
    "price_365": "Цена 💎 на 365 дней, ₽ (0 — не продавать)",
    "channel_hour": "Час публикации статистики в канале",
    "niche_daily_limit": "Предложений новых ниш от пользователя в сутки",
    "ref_limit": "Сколько приглашений на человека награждается (всего)",
    "bonus_max_days": "Потолок бонусных дней вперёд (бонусы и приглашения)",
}


async def get(session: AsyncSession, key: str) -> str:
    row = await session.get(Setting, key)
    return row.value if row else DEFAULTS[key]


async def get_int(session: AsyncSession, key: str) -> int:
    return int(await get(session, key))


async def put(session: AsyncSession, key: str, value: str) -> None:
    row = await session.get(Setting, key)
    if row:
        row.value = value
    else:
        session.add(Setting(key=key, value=value))
    await session.commit()
