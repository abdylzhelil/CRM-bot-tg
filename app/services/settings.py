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
    "cleanup_days": "7",
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
    "cleanup_days": "Удалять не завершивших регистрацию через N дней (0 — не удалять)",
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


# Разделы меню «⚙️ Настройки»: (значок, название, [ключи])
SECTIONS: list[tuple[str, str, list[str]]] = [
    ("💎", "Тарифы и цены", ["price_30", "price_90", "price_365", "free_daily_limit", "free_delay_min"]),
    ("📥", "Раздача заявок", ["max_recipients", "share_gap_min", "pass_daily_limit"]),
    ("🎁", "Бонусы", ["bonus_leads", "bonus_max_days", "ref_days", "ref_percent", "ref_limit"]),
    ("🌐", "Каталог и каналы", ["submit_daily_limit", "catalog_min_members", "niche_daily_limit",
                               "channel_poll_min", "channel_hour"]),
    ("🔍", "Разведчик", ["mode", "min_members", "min_msgs_per_day", "default_join_limit", "search_interval_h",
                        "rotation_days"]),
    ("🛡", "Антиспам", ["spam_suspicious", "spam_block", "dedupe_days", "spam_votes_to_ban", "cleanup_days"]),
    ("📊", "Отчёты", ["report_hour"]),
]

# Короткие подписи для кнопок (полные — в LABELS, их видно при редактировании)
SHORT: dict[str, str] = {
    "price_30": "30 дней, ₽", "price_90": "90 дней, ₽", "price_365": "365 дней, ₽",
    "free_daily_limit": "Бесплатный: заявок/день", "free_delay_min": "Бесплатный: задержка, мин",
    "max_recipients": "Получателей на заявку", "share_gap_min": "Интервал очереди, мин",
    "pass_daily_limit": "Передач коллегам/день",
    "bonus_leads": "Заявок за +1 день", "bonus_max_days": "Потолок бонуса, дней",
    "ref_days": "За друга, дней", "ref_percent": "% от бонусов друга", "ref_limit": "Макс. приглашений",
    "submit_daily_limit": "Групп от человека/день", "catalog_min_members": "Мин. участников в группе",
    "niche_daily_limit": "Своих ниш/день", "channel_poll_min": "Опрос каналов, мин",
    "channel_hour": "Статистика в канал, час",
    "mode": "Режим вступления", "min_members": "Мин. участников", "min_msgs_per_day": "Мин. сообщений/сутки",
    "default_join_limit": "Вступлений/сутки", "search_interval_h": "Поиск раз в, ч",
    "rotation_days": "Выход из тихих групп, дн",
    "spam_suspicious": "Порог «подозрительный»", "spam_block": "Порог «спам»",
    "dedupe_days": "Окно дублей, дн", "cleanup_days": "Удалять пустых через, дн", "spam_votes_to_ban": "Голосов «спам» для бана",
    "report_hour": "Час вечернего отчёта",
}
