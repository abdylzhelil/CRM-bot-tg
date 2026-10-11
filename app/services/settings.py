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
