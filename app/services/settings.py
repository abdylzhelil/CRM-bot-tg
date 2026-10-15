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
    "join_retry_days": "3",
    "join_retry_max": "4",
    "adpost_enabled": "0",
    "adpost_auto_approve": "0",
    "adpost_auto_refund": "1",
    "adpost_fulfill": "1",
    "adpost_price": "0",
    "adpost_daily_limit": "5",
    "adpost_group_daily": "3",
    "adpost_gap_min": "10",
    "adpost_footer": "Реклама",
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
    "refund_days": "3",
    "ad_leads": "20",
    "ad_days": "7",
    "autorenew": "0",
    "license_enabled": "0",
    "license_leads": "50",
    "license_days": "30",
    "ai_enabled": "0",
    "ai_model": "claude-haiku-5-5",
    "ai_free_daily": "3",
    "ai_paid_daily": "30",
    "channel_hour": "12",
    "niche_daily_limit": "3",
    "ref_limit": "10",
    "bonus_max_days": "180",
    "cleanup_days": "7",
    "cleanup_warn_days": "1",
    "lead_keep_days": "90",
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
    "join_retry_days": "Повторять заявку на вступление через N дней",
    "join_retry_max": "Сколько раз пробовать вступить в группу",
    "adpost_enabled": "Реклама в группах через разведчиков (1/0)",
    "adpost_auto_approve": "Реклама в группах: одобрять заказы без запрещённых слов сами (1/0)",
    "adpost_auto_refund": "Реклама в группах: сами возвращать деньги за группы, где пост не вышел или удалён (1/0)",
    "adpost_fulfill": "Реклама в группах: разведчик выполняет условия групп — подписка на канал, приглашение своих разведчиков (1/0)",
    "adpost_price": "Реклама в группах: цена за одну группу, ₽ (0 — бесплатно)",
    "adpost_daily_limit": "Реклама в группах: постов в сутки с одного аккаунта",
    "adpost_group_daily": "Реклама в группах: постов в сутки в одной группе",
    "adpost_gap_min": "Реклама в группах: пауза между постами аккаунта, мин",
    "adpost_footer": "Реклама в группах: подпись под постом (- без подписи)",
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
    "refund_days": "Возврат денег в течение N дней после оплаты (0 — без возврата)",
    "ad_leads": "Реклама группы: сколько её заявок стоит (0 — реклама выключена)",
    "ad_days": "Реклама группы: на сколько дней",
    "autorenew": "Автопродление тарифа при оплате по ссылке (1 — включено; нужно разрешение ЮKassa)",
    "license_enabled": "Лицензия бота модерации за заявки группы (1 — включено)",
    "license_leads": "Лицензия: заявок группы за один период",
    "license_days": "Лицензия: дней за период",
    "ai_enabled": "Claude для черновиков (1 — включено, платно; 0 — простой бесплатный черновик по правилам)",
    "ai_model": "Модель ИИ (claude-haiku-5-5 — рекомендуем, самая дешёвая; claude-sonnet-5-5; claude-opus-5-5)",
    "ai_free_daily": "ИИ: запросов в сутки на бесплатном тарифе",
    "ai_paid_daily": "ИИ: запросов в сутки на платном тарифе",
    "channel_hour": "Час публикации статистики в канале",
    "niche_daily_limit": "Предложений новых ниш от пользователя в сутки",
    "ref_limit": "Сколько приглашений на человека награждается (всего)",
    "bonus_max_days": "Потолок бонусных дней вперёд (бонусы и приглашения)",
    "cleanup_days": "Удалять не завершивших регистрацию через N дней (0 — не удалять)",
    "cleanup_warn_days": "Предупреждать об удалении за N дней (0 — не предупреждать)",
    "lead_keep_days": "Хранить заявки N дней, потом удалять (0 — хранить всегда)",
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
    ("💎", "Тарифы и цены", ["price_30", "price_90", "price_365", "refund_days", "autorenew", "free_daily_limit", "free_delay_min"]),
    ("📥", "Раздача заявок", ["max_recipients", "share_gap_min", "pass_daily_limit"]),
    ("🎁", "Бонусы", ["bonus_leads", "bonus_max_days", "ref_days", "ref_percent", "ref_limit"]),
    ("🌐", "Каталог и каналы", ["submit_daily_limit", "catalog_min_members", "niche_daily_limit",
                               "channel_poll_min", "channel_hour", "ad_leads", "ad_days", "license_enabled", "license_leads", "license_days"]),
    ("🔍", "Разведчик", ["mode", "min_members", "min_msgs_per_day", "default_join_limit", "search_interval_h",
                        "rotation_days", "join_retry_days", "join_retry_max",
                        "adpost_enabled", "adpost_auto_approve", "adpost_auto_refund", "adpost_fulfill", "adpost_price", "adpost_daily_limit", "adpost_group_daily", "adpost_gap_min", "adpost_footer"]),
    ("🛡", "Антиспам", ["spam_suspicious", "spam_block", "dedupe_days", "spam_votes_to_ban", "cleanup_days", "cleanup_warn_days", "lead_keep_days"]),
    ("✨", "ИИ", ["ai_enabled", "ai_model", "ai_free_daily", "ai_paid_daily"]),
    ("📊", "Отчёты", ["report_hour"]),
]

# Короткие подписи для кнопок (полные — в LABELS, их видно при редактировании)
SHORT: dict[str, str] = {
    "price_30": "30 дней, ₽", "price_90": "90 дней, ₽", "price_365": "365 дней, ₽", "refund_days": "Срок возврата, дн", "autorenew": "Автопродление (1/0)",
    "free_daily_limit": "Бесплатный: заявок/день", "free_delay_min": "Бесплатный: задержка, мин",
    "max_recipients": "Получателей на заявку", "share_gap_min": "Интервал очереди, мин",
    "pass_daily_limit": "Передач коллегам/день",
    "bonus_leads": "Заявок за +1 день", "bonus_max_days": "Потолок бонуса, дней",
    "ref_days": "За друга, дней", "ref_percent": "% от бонусов друга", "ref_limit": "Макс. приглашений",
    "submit_daily_limit": "Групп от человека/день", "catalog_min_members": "Мин. участников в группе",
    "niche_daily_limit": "Своих ниш/день", "ad_leads": "Реклама: цена в заявках", "ad_days": "Реклама: дней", "license_enabled": "Лицензия модерации (1/0)", "license_leads": "Лицензия: заявок", "license_days": "Лицензия: дней", "channel_poll_min": "Опрос каналов, мин",
    "channel_hour": "Статистика в канал, час",
    "mode": "Режим вступления", "min_members": "Мин. участников", "min_msgs_per_day": "Мин. сообщений/сутки",
    "default_join_limit": "Вступлений/сутки", "search_interval_h": "Поиск раз в, ч",
    "rotation_days": "Выход из тихих групп, дн",
    "join_retry_days": "Повтор заявки, дн",
    "join_retry_max": "Попыток вступить",
    "adpost_enabled": "Реклама вкл (1/0)", "adpost_auto_approve": "Автоодобрение (1/0)", "adpost_auto_refund": "Автовозврат (1/0)", "adpost_fulfill": "Условия групп (1/0)", "adpost_price": "Цена за группу, ₽", "adpost_daily_limit": "Постов в сутки",
    "adpost_group_daily": "Постов в группу в сутки",
    "adpost_gap_min": "Пауза, мин", "adpost_footer": "Подпись",
    "spam_suspicious": "Порог «подозрительный»", "spam_block": "Порог «спам»",
    "dedupe_days": "Окно дублей, дн", "cleanup_days": "Удалять пустых через, дн", "cleanup_warn_days": "Предупредить за, дн", "spam_votes_to_ban": "Голосов «спам» для бана", "lead_keep_days": "Хранить заявки, дн",
    "report_hour": "Час вечернего отчёта",
    "ai_enabled": "Claude (1/0)", "ai_model": "Модель ИИ", "ai_free_daily": "ИИ в сутки: бесплатные",
    "ai_paid_daily": "ИИ в сутки: платные",
}


# Подсказка к каждой настройке: что это и на что влияет (показывается при редактировании)
HINTS: dict[str, str] = {
    "mode": "semi — разведчик спрашивает вас о вступлении, auto — вступает сам.",
    "min_members": "Группы меньше этого размера разведчик пропускает.",
    "min_msgs_per_day": "Тихие группы (меньше сообщений в сутки) не берём — заявок там не будет.",
    "default_join_limit": "Сколько групп один аккаунт вступает в сутки. Больше — выше риск блокировки аккаунта.",
    "search_interval_h": "Как часто разведчик ищет новые группы.",
    "spam_suspicious": "Сообщение с оценкой выше — помечается «подозрительным».",
    "spam_block": "Сообщение с оценкой выше — считается спамом и не раздаётся.",
    "dedupe_days": "Одинаковые сообщения за это окно считаются повтором.",
    "rotation_days": "Группу без заявок столько дней разведчик покидает и ищет новую.",
    "join_retry_days": "Если админ группы не одобрил заявку на вступление, разведчик отправит её снова через столько дней.",
    "join_retry_max": "Сколько всего раз разведчик пробует вступить в группу, где нужно одобрение админа. После этого группа пропускается.",
    "adpost_enabled": "Включает заказ рекламы пользователями и публикацию постов разведчиками в разрешённых группах. 0 — ничего не публикуется.",
    "adpost_auto_approve": "1 — заказ без запрещённых слов сразу идёт на оплату, без вашей проверки (подозрительные всё равно придут вам). 0 — вы одобряете каждый заказ.",
    "adpost_fulfill": "1 — если группа требует подписаться на канал или пригласить людей (пишет бот группы, описание или закреп), разведчик подписывается сам и добавляет в группу наших других разведчиков (не больше 5 приглашений в сутки с аккаунта), потом пробует пост снова. Капчи и кнопки «я не бот» не нажимает. 0 — ничего не делать.",
    "adpost_auto_refund": "1 — когда заказ закончен, бот сам возвращает через ЮKassa деньги за группы, где пост не вышел или его удалили модераторы (вам и заказчику придёт сообщение). 0 — возвращаете сами кнопкой «💸 Вернуть за невышедшие».",
    "adpost_price": "Сколько рублей стоит один пост в одной группе. 0 — реклама бесплатна (только с одобрением админа).",
    "adpost_daily_limit": "Сколько рекламных постов в сутки публикует один аккаунт-разведчик. Меньше — безопаснее для аккаунта.",
    "adpost_group_daily": "Сколько рекламных постов в сутки может выйти в одной группе или канале, чтобы их не засыпать рекламой.",
    "adpost_gap_min": "Минимальная пауза между рекламными постами одного аккаунта. Слишком частые посты — риск блокировки.",
    "adpost_footer": "Текст под каждым рекламным постом (например, «Реклама. ИП …, erid …» по требованиям закона). «-» — без подписи.",
    "report_hour": "Во сколько вам приходит вечерний отчёт.",
    "max_recipients": "Больше людей на одну заявку — меньше шанс, что клиент ответит каждому.",
    "free_daily_limit": "Сколько заявок в день видит бесплатный пользователь.",
    "free_delay_min": "Бесплатные получают заявку позже платных на это время.",
    "spam_votes_to_ban": "Сколько пользователей должны пожаловаться, чтобы автор попал в общий чёрный список.",
    "submit_daily_limit": "Защита от заваливания каталога группами.",
    "catalog_min_members": "Добавленные в каталог группы меньше этого размера отклоняются.",
    "bonus_leads": "Сколько заявок из группы автора даёт ему +1 день 💎.",
    "channel_poll_min": "Как часто опрашиваются открытые каналы.",
    "ref_days": "Сколько дней 💎 получают приглашающий и приглашённый.",
    "ref_percent": "Процент от бонусных и купленных дней приглашённого, который получает пригласивший.",
    "pass_daily_limit": "Сколько заявок человек может передать коллегам за сутки.",
    "share_gap_min": "Интервал в очереди, когда одну заявку получают несколько человек.",
    "price_30": "0 — пакет не продаётся.",
    "price_90": "0 — пакет не продаётся.",
    "price_365": "0 — пакет не продаётся.",
    "refund_days": "Через сколько дней после оплаты возврат уже невозможен. 0 — возвраты выключены.",
    "ad_leads": "Сколько заявок группы админ тратит на неделю рекламы. 0 — реклама выключена.",
    "ad_days": "На сколько дней покупается реклама.",
    "autorenew": "Включать только после разрешения ЮKassa на сохранение способа оплаты.",
    "license_enabled": "Лицензия бота модерации за заявки группы. Бот модерации сам это не проверяет.",
    "license_leads": "Сколько заявок группы нужно за один период лицензии.",
    "license_days": "На сколько дней продлевается лицензия.",
    "ai_enabled": "Платно! Нужен ANTHROPIC_API_KEY в .env. 0 — бесплатный черновик по правилам.",
    "ai_model": "Haiku — самая дешёвая, для коротких черновиков её обычно хватает. Sonnet примерно в 20 раз дороже, Opus примерно в 40 раз. Сравнить качество на ваших заявках: команда /aitest.",
    "ai_free_daily": "Лимит на бесплатном тарифе, чтобы не тратить деньги.",
    "ai_paid_daily": "Лимит на платном тарифе.",
    "channel_hour": "Во сколько публикуется сводка в канал.",
    "niche_daily_limit": "Защита от заваливания предложениями ниш.",
    "ref_limit": "Максимум наград за приглашения на одного человека.",
    "bonus_max_days": "Больше этого бонусами не накопить (купленные дни — без потолка).",
    "cleanup_days": "Незавершившие регистрацию удаляются через столько дней. 0 — не удалять.",
    "cleanup_warn_days": "За сколько дней до удаления прислать предупреждение. 0 — без предупреждения.",
    "lead_keep_days": "Заявки содержат тексты и ники людей из групп. Через столько дней они удаляются (кроме тех, что взяты в работу). 0 — не удалять.",
}

# Быстрые наборы значений: (значок, название, описание, {ключ: значение})
PRESETS: dict[str, tuple[str, str, str, dict[str, str]]] = {
    "launch": ("🚀", "Запуск", "Щедрее к новичкам: больше заявок бесплатно, выше бонусы за друзей.", {
        "free_daily_limit": "7", "free_delay_min": "20", "ref_days": "10", "bonus_leads": "3",
        "cleanup_days": "7", "cleanup_warn_days": "1",
    }),
    "careful": ("🛡", "Осторожно", "Меньше риска и расходов: разведчик медленнее, бесплатный тариф скромнее, ИИ и автопродление выключены.", {
        "default_join_limit": "5", "search_interval_h": "12", "spam_suspicious": "25", "spam_block": "50",
        "free_daily_limit": "3", "free_delay_min": "60", "ref_days": "3", "bonus_leads": "10",
        "ai_enabled": "0", "autorenew": "0", "license_enabled": "0",
    }),
}


async def preset_diff(session: AsyncSession, name: str) -> list[tuple[str, str, str]]:
    """Что поменяет набор: [(ключ, сейчас, станет)] — только отличающиеся значения."""
    out = []
    for key, new in PRESETS[name][3].items():
        cur = await get(session, key)
        if cur != new:
            out.append((key, cur, new))
    return out


async def apply_preset(session: AsyncSession, name: str) -> int:
    diff = await preset_diff(session, name)
    for key, _, new in diff:
        await put(session, key, new)
    return len(diff)


def search(query: str) -> list[str]:
    """Ключи настроек, у которых подпись, название или подсказка содержат все слова запроса."""
    words = [w for w in query.lower().split() if w]
    if not words:
        return []
    found = []
    for key, label in LABELS.items():
        hay = f"{key} {label} {SHORT.get(key, '')} {HINTS.get(key, '')}".lower()
        if all(w in hay for w in words):
            found.append(key)
    return found[:10]


class SettingError(ValueError):
    """Понятная причина, почему значение настройки не подходит."""


# Переключатели: только 0 или 1 (иначе «2» молча считалось бы «выключено»)
FLAGS = {"autorenew", "license_enabled", "ai_enabled", "scout_paused", "adpost_enabled", "adpost_auto_approve", "adpost_auto_refund", "adpost_fulfill"}
# Разумные границы: опечатка (0 или лишний ноль) не должна ломать работу бота
RANGES: dict[str, tuple[int, int]] = {
    "max_recipients": (1, 50),          # 0 — заявки перестали бы доходить до людей
    "search_interval_h": (1, 168),      # 0 — разведчик искал бы без пауз
    "channel_poll_min": (1, 1440),      # 0 — бесконечные запросы к t.me
    "rotation_days": (1, 365),          # 0 — разведчик вышел бы из всех групп
    "join_retry_days": (1, 60), "join_retry_max": (1, 20), "adpost_price": (0, 100_000), "adpost_daily_limit": (1, 50), "adpost_group_daily": (1, 50),
    "adpost_gap_min": (1, 1440),  # 0 — заявку не пробовали бы вовсе
    "spam_suspicious": (0, 100), "spam_block": (0, 100),
    "ref_percent": (0, 100), "bonus_leads": (1, 1000), "free_delay_min": (0, 1440), "free_daily_limit": (0, 1000),
    "share_gap_min": (0, 60), "refund_days": (0, 60), "ad_days": (1, 365), "license_days": (1, 365),
    "cleanup_days": (0, 365), "cleanup_warn_days": (0, 30), "lead_keep_days": (0, 3650), "default_join_limit": (1, 50),
    "price_30": (0, 1_000_000), "price_90": (0, 1_000_000), "price_365": (0, 1_000_000),
}


def validate(key: str, text: str) -> str:
    """Проверяет и нормализует значение настройки (одни правила для бота и кабинета). Возвращает строку для записи."""
    text = str(text).strip()
    if key not in LABELS:
        raise SettingError("Такой настройки нет")
    if key == "mode":
        if text not in ("semi", "auto"):
            raise SettingError("Нужно semi или auto")
    elif key in ("report_hour", "channel_hour"):
        if not text.isdigit() or int(text) > 23:
            raise SettingError("Нужен час от 0 до 23")
    elif key == "ai_model":
        from app.services.ai import MODELS

        if text not in MODELS:
            raise SettingError("Допустимо: " + ", ".join(MODELS))
    elif key == "adpost_footer":
        if len(text) > 200:
            raise SettingError("Не длиннее 200 знаков")
        return "" if text == "-" else text
    elif key in FLAGS:
        if text not in ("0", "1"):
            raise SettingError("Нужно 0 (выключено) или 1 (включено)")
    elif not (text.isascii() and text.isdigit()) or len(text) > 9:  # «²» и прочие «цифры» Юникода — не число
        raise SettingError("Нужно число")
    elif key in RANGES and not RANGES[key][0] <= int(text) <= RANGES[key][1]:
        lo, hi = RANGES[key]
        raise SettingError(f"Допустимо от {lo} до {hi}")
    return text
