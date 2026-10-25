"""Город и регион заявки: из текста, затем из названия группы, затем из карточки группы."""
from functools import lru_cache

from app.services.text import has_phrase, lemmas

# город -> регион. Порядок важен только для отображения в меню выбора.
CITIES: dict[str, str] = {
    "Махачкала": "Дагестан",
    "Каспийск": "Дагестан",
    "Дербент": "Дагестан",
    "Хасавюрт": "Дагестан",
    "Буйнакск": "Дагестан",
    "Избербаш": "Дагестан",
    "Кизляр": "Дагестан",
    "Кизилюрт": "Дагестан",
    "Москва": "Москва",
    "Санкт-Петербург": "Санкт-Петербург",
    "Грозный": "Чечня",
    "Нальчик": "Кабардино-Балкария",
    "Владикавказ": "Северная Осетия",
    "Ставрополь": "Ставропольский край",
    "Краснодар": "Краснодарский край",
    "Сочи": "Краснодарский край",
    "Ростов-на-Дону": "Ростовская область",
    "Астрахань": "Астраханская область",
    "Волгоград": "Волгоградская область",
    "Казань": "Татарстан",
    "Екатеринбург": "Свердловская область",
    "Новосибирск": "Новосибирская область",
}
REGIONS = sorted(set(CITIES.values()))
# сокращения и разговорные названия
ALIASES: dict[str, str] = {
    "мхк": "Махачкала", "махачкале": "Махачкала", "мск": "Москва", "спб": "Санкт-Петербург",
    "питер": "Санкт-Петербург", "ростов": "Ростов-на-Дону",
}
REGION_ALIASES: dict[str, str] = {"рд": "Дагестан", "дагестан": "Дагестан", "чечня": "Чечня", "кбр": "Кабардино-Балкария"}

KNOWN_PLACES = list(CITIES) + [r for r in REGIONS if r not in CITIES]  # для подсказки при опечатке

# кнопки выбора города при регистрации
MENU_CITIES = ["Махачкала", "Каспийск", "Дербент", "Хасавюрт", "Дагестан", "Москва", "Санкт-Петербург"]


@lru_cache(maxsize=1)
def _phrases() -> list[tuple[list[str], str, str]]:
    """(леммы, город, регион); город пуст, если фраза обозначает только регион."""
    out = [(lemmas(city), city, region) for city, region in CITIES.items()]
    out += [(lemmas(alias), city, CITIES[city]) for alias, city in ALIASES.items()]
    out += [(lemmas(alias), "", region) for alias, region in REGION_ALIASES.items()]
    return [p for p in out if p[0]]


def find(text: str) -> tuple[str, str]:
    lem = lemmas(text)
    region_only = ""
    for phrase, city, region in _phrases():
        if has_phrase(lem, phrase):
            if city:
                return city, region
            region_only = region_only or region
    return "", region_only


def detect(text: str, group_title: str = "", group_city: str = "", group_region: str = "") -> tuple[str, str]:
    for source in (text, group_title):
        city, region = find(source) if source else ("", "")
        if city or region:
            return city, region
    if group_city:
        return group_city, CITIES.get(group_city, group_city if group_city in REGIONS else group_region)
    if group_region:  # группа региона (определил разведчик при проверке группы)
        return "", group_region
    return "", ""


# «от Дагестана дальше» (владелец 25.10): порядок поиска и вступления — свой регион, соседи, Россия, заграница
NEIGHBORS: dict[str, tuple[str, ...]] = {
    "Дагестан": ("Чечня", "Кабардино-Балкария", "Северная Осетия", "Ставропольский край", "Астраханская область"),
}
_FOREIGN = ("паттай", "таиланд", "тайланд", "пхукет", "бали", "дубай", "оаэ", "турци", "анталь", "алани", "стамбул",
            "грузи", "тбилиси", "батуми", "казахстан", "алмат", "астан", "бишкек", "ташкент", "ереван", "армени",
            "минск", "беларус", "кипр", "израил", "египет", "хургад", "шарм", "вьетнам", "нячанг", "сша", "америк",
            "европ", "германи", "польш", "черногор", "серби", "белград", "испани", "итали", "франци", "лондон",
            "чехи", "праг", "финлянд", "эстони", "латви", "литв", "китай", "корея", "япони", "абхази", "сочи-адлер")


def home_cities(home: str) -> list[str]:
    return [c for c, r in CITIES.items() if r == home]


def is_foreign(title: str) -> bool:
    low = (title or "").lower()
    return any(w in low for w in _FOREIGN)


def priority(city: str, region: str, title: str, home: str) -> int:
    """0 — свой регион, 1 — соседи, 2 — Россия (в т. ч. без города), 3 — заграница. Меньше — раньше."""
    region = region or region_of(city)
    if not region and not city:
        city, region = find(title or "")
    if home and (region == home or city in home_cities(home)):
        return 0
    if region and region in NEIGHBORS.get(home, ()):
        return 1
    if is_foreign(title) and not region:
        return 3
    return 2


def region_of(place: str) -> str:
    return CITIES.get(place, place if place in REGIONS else "")


def matches(user_city: str, only_city: bool, lead_city: str, lead_region: str) -> bool:
    """Подходит ли заявка пользователю по месту.

    Пусто у пользователя — вся Россия. Выбран регион — все его города.
    Выбран город — этот город и заявки, где указан только его регион.
    Заявки без места приходят, если пользователь не включил «только мой город».
    """
    if not user_city:
        return True
    if not lead_city and not lead_region:
        return not only_city
    if user_city in REGIONS and user_city not in CITIES:
        return lead_region == user_city
    if lead_city:
        return lead_city == user_city
    return lead_region == region_of(user_city)
