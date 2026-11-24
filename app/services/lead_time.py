"""Display the time we actually know. Lead.created_at is ingestion, not publication."""
from datetime import datetime, timezone, timedelta
from app.models import now

MOSCOW = timezone(timedelta(hours=3))
PUBLICATION_NOTE = 'Дата публикации не сохранена. Если оригинал доступен, посмотрите дату в нём.'


def found_label(lead, reference=None):
    value = getattr(lead, 'created_at', None)
    if not isinstance(value, datetime):
        return '🕒 Время обнаружения не сохранено'
    reference = reference or now()
    utc = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    ref = reference.replace(tzinfo=timezone.utc) if reference.tzinfo is None else reference.astimezone(timezone.utc)
    local = utc.astimezone(MOSCOW)
    day = (ref.astimezone(MOSCOW).date()-local.date()).days
    prefix = 'сегодня, ' if day == 0 else 'вчера, ' if day == 1 else ''
    seconds = int((ref-utc).total_seconds())
    if seconds < -60:
        age = 'время требует проверки'
    elif seconds < 60:
        age = 'только что'
    elif seconds < 3600:
        age = f'{seconds//60} мин. назад'
    elif seconds < 86400:
        age = f'{seconds//3600} ч. назад'
    else:
        age = f'{seconds//86400} дн. назад'
    return f'🕒 Найдена ботом: {prefix}{local:%d.%m.%Y в %H:%M} МСК · {age}'
