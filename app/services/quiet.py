"""Режим тишины и признак «горячей» заявки (без ИИ — по словам в тексте)."""
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app import config

_HOT = re.compile(
    r"(срочн\w*|сегодня|прямо сейчас|до завтра|завтра|горит|asap|"
    r"бюджет\w*|готов\w* (оплатить|заплатить)|оплач\w*|\d+\s*(к|т\.?р|тыс\w*|000|₽|руб\w*))",
    re.I,
)


def is_hot(text: str) -> bool:
    return bool(_HOT.search(text))


def local_now() -> datetime:
    return datetime.now(ZoneInfo(config.TZ_NAME))


def to_local(moment: datetime) -> datetime:
    """Время из базы (UTC без пояса) → местное."""
    return moment.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(config.TZ_NAME))


def parse_hours(raw: str) -> tuple[int, int] | None:
    """«23-8» -> (23, 8); пусто или «-» — режим выключен."""
    m = re.fullmatch(r"\s*(\d{1,2})\s*-\s*(\d{1,2})\s*", raw or "")
    if not m:
        return None
    start, end = int(m[1]), int(m[2])
    if start > 23 or end > 23 or start == end:
        return None
    return start, end


def in_quiet(hour: int, hours: tuple[int, int] | None) -> bool:
    if not hours:
        return False
    start, end = hours
    return start <= hour < end if start < end else hour >= start or hour < end
