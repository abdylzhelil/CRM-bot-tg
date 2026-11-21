"""Морфологический поиск триггерных слов: «сайт» находит «сайта», «сайтом» и т.д."""
import re
from dataclasses import dataclass, field
from functools import lru_cache

import pymorphy3

_morph = pymorphy3.MorphAnalyzer()
_TOKEN = re.compile(r"\w+", re.UNICODE)


@lru_cache(maxsize=50_000)
def _lemma(token: str) -> str:
    return _morph.parse(token)[0].normal_form


def tokens(text: str) -> list[str]:
    return _TOKEN.findall(text.lower().replace("ё", "е"))


def lemmas(text: str) -> list[str]:
    return [_lemma(t) for t in tokens(text)]


@lru_cache(maxsize=10_000)
def _first_person_verb(token: str) -> bool:
    tag = _morph.parse(token)[0].tag
    return "VERB" in tag and "1per" in tag


# Минус-фраза: (точная форма?, токены). Глагол от 1-го лица («делаю», «предлагаю») сравнивается
# точно, иначе «делаю» отсекало бы клиентские «кто делает сайты?». Остальное — по леммам.
Negative = tuple[bool, list[str]]


def compile_negatives(words: list[str]) -> list[Negative]:
    out: list[Negative] = []
    for w in words:
        raw = tokens(w)
        if len(raw) == 1 and _first_person_verb(raw[0]):
            out.append((True, raw))
        elif raw:
            out.append((False, [_lemma(t) for t in raw]))
    return out


def negative_hit(lem: list[str], raw: list[str], negatives: list[Negative]) -> bool:
    return any(has_phrase(raw if exact else lem, phrase) for exact, phrase in negatives)


def has_phrase(haystack: list[str], phrase: list[str]) -> bool:
    n = len(phrase)
    if n == 0 or n > len(haystack):
        return False
    return any(haystack[i : i + n] == phrase for i in range(len(haystack) - n + 1))


@dataclass
class NodeRules:
    id: int
    parent_id: int | None
    depth: int
    name: str
    city: list[str] = field(default_factory=list)  # леммы города
    words: list[list[str]] = field(default_factory=list)
    strong: list[list[str]] = field(default_factory=list)
    negatives: list[Negative] = field(default_factory=list)


@dataclass
class Rules:
    intents: list[list[str]] = field(default_factory=list)
    negatives: list[Negative] = field(default_factory=list)
    nodes: list[NodeRules] = field(default_factory=list)


@dataclass
class Match:
    category_id: int
    category_name: str
    hits: list[str]


@dataclass
class TaskMatch:
    match: Match
    text: str
    place_text: str


def compile_phrases(words: list[str]) -> list[list[str]]:
    return [p for p in (lemmas(w) for w in words) if p]


def _property_request(raw: list[str], lem: list[str]) -> bool:
    """«Снять обои в квартире» — не поиск жилья или помещения.

    Проверяем объект намерения, а не само присутствие адреса работ.
    Это только ограничение совпадений: настроенные слова/минусы остаются обязательными.
    """
    objects = {"квартира", "помещение", "офис", "дом", "комната", "жильё", "жилье",
               "склад", "участок", "гараж", "недвижимость", "студия"}
    actions = {"снять", "арендовать", "купить", "сдать", "продать", "искать", "нужный"}
    for i, word in enumerate(lem):
        if word not in objects:
            continue
        # «в квартире», «из помещения» описывают место услуги.
        if i and lem[i - 1] in {"в", "на", "из", "у", "по"}:
            continue
        # Объект сразу рядом с намерением, допускаем определения: «ищу небольшое помещение».
        for j in range(max(0, i - 3), min(len(lem), i + 4)):
            if lem[j] not in actions | {"аренда", "покупка", "продажа"}:
                continue
            between = raw[min(i, j) + 1:max(i, j)]
            if all("ADJF" in _morph.parse(t)[0].tag or t.isdigit() for t in between):
                return True
    return False


@lru_cache(maxsize=10_000)
def _object_modifier(word: str) -> bool:
    tag = _morph.parse(word)[0].tag
    return word.isdigit() or "ADJF" in tag or "NUMR" in tag


def _service_aliases(text: str) -> set[tuple[str, ...]]:
    """Уточнения действующих ключевых слов, не новые категории/сильные фразы.

    Пара действие–предмет должна быть рядом, в одной фразе. «Снять видео
    про обои» и «собрать документы для шкафа» не означают демонтаж/сборку.
    """
    furniture = {"мебель", "стол", "шкаф", "комод", "кровать", "тумба", "кухня", "стеллаж", "диван"}
    cargo = furniture | {"холодильник", "техника", "стиральный", "дрова", "доска", "мешок", "коробка",
                         "стройматериал", "кирпич", "цемент", "песок", "щебень", "газоблок", "арматура",
                         "вещь", "бутылка", "книга"}
    loading = {"разгрузить", "разгружать", "разгрузка", "выгрузить", "выгрузка", "погрузить", "погрузка", "загрузить"}
    variants = [
        (("сборка", "мебель"), {"собрать", "собирать", "сборка"}, furniture),
        (("грузчик",), loading | {"занести", "перенести", "вынести",
                        "поднять", "спустить", "перетаскать"}, cargo),
        (("грузчик",), loading, {"машина", "фура", "грузовик"}),
        (("демонтаж",), {"снять", "снимать", "демонтировать", "удалить", "сбить", "разобрать"},
         {"обои", "плитка", "плинтус", "штукатурка", "линолеум", "перегородка", "стена", "доска", "навес"}),
        (("демонтаж",), {"демонтировать", "разобрать"}, {"сарай"}),
    ]
    found = set()
    for clause in re.split(r"[.!?;\n]+", text):
        raw = tokens(clause)
        lem = [_lemma(t) for t in raw]
        for key, actions, objects in variants:
            for i, word in enumerate(lem):
                if word not in actions or _first_person_verb(raw[i]):
                    continue
                for j in range(i + 1, min(i + 5, len(lem))):
                    if lem[j] in objects and all(_object_modifier(t) for t in raw[i + 1:j]):
                        found.add(key)
    return found


def _without_meal_perks(lem: list[str]) -> list[str]:
    """«С меня обед» — бонус помощнику, а не заказ обедов."""
    out = lem.copy()
    for phrase in (("с", "я", "обед"), ("обед", "за", "мой", "счёт")):
        for i in range(len(lem) - len(phrase) + 1):
            if tuple(lem[i:i + len(phrase)]) == phrase:
                out[i:i + len(phrase)] = [""] * len(phrase)
    return out


def match(text: str, rules: Rules, context: str = "") -> Match | None:
    """(слово-намерение + слово категории) ИЛИ сильная фраза категории, без минус-слов.

    context — название группы: нужен для категорий с городом (Недвижимость → Махачкала).
    """
    raw = tokens(text)
    lem = [_lemma(t) for t in raw]
    if not lem:
        return None
    if negative_hit(lem, raw, rules.negatives):
        return None
    has_intent = any(has_phrase(lem, p) for p in rules.intents)
    ctx = lemmas(context) if context else []
    aliases = _service_aliases(text) if has_intent else set()
    meal_lem = _without_meal_perks(lem)

    best: tuple[int, int, Match] | None = None
    explicit_places = None
    for node in rules.nodes:
        if node.name.split(" → ")[0] == "Недвижимость" and not _property_request(raw, lem):
            continue
        if negative_hit(lem, raw, node.negatives):
            continue
        if node.city:
            if explicit_places is None:
                from app.services import geo
                explicit_places = geo.request_places(text)
            cities = [city for city, _ in explicit_places if city]
            if cities:
                if not any(has_phrase(lemmas(city + " " + region), node.city) for city, region in explicit_places if city):
                    continue  # явный город заказа важнее названия группы
            elif explicit_places:
                regions = {region for city, region in geo.CITIES.items() if has_phrase(lemmas(city), node.city)}
                if not any(region in regions or has_phrase(lemmas(region), node.city) for _, region in explicit_places):
                    continue
                if not any(has_phrase(lemmas(region), node.city) for _, region in explicit_places) and not has_phrase(ctx, node.city):
                    continue  # один регион не расширяет настроенный город категории
            elif any(has_phrase(lemmas(city), node.city) for city in geo.CITIES):
                if not has_phrase(ctx, node.city):
                    continue  # название часового пояса в исходном тексте не адрес
            elif not (has_phrase(lem, node.city) or has_phrase(ctx, node.city)):
                continue
        word_lem = meal_lem if node.name.split(" → ")[0] == "Еда на заказ" else lem
        words = [p for p in node.words if has_phrase(word_lem, p)]
        contextual = [p for p in node.words if tuple(p) in aliases
                      and not negative_hit(p, p, rules.negatives)
                      and not negative_hit(p, p, node.negatives)]
        inferred = [p for p in contextual if p not in words]
        strong = [p for p in node.strong if has_phrase(lem, p)]
        if not strong and not (has_intent and (words or inferred)):
            continue
        # Действие с конкретным предметом точнее одного упоминания предмета.
        score = len(words) + len(inferred) + 2 * len(contextual) + 2 * len(strong)
        key = (score, node.depth)
        if best is None or key > best[:2]:
            # Сохраняем канонические слова: статистика «Не моё» должна найти
            # исходный Keyword и дать владельцу выключить его существующей кнопкой.
            hits = [" ".join(p) for p in words + strong + inferred]
            best = (*key, Match(node.id, node.name, hits))
    return best[2] if best else None


def match_tasks(text: str, rules: Rules, context: str = "") -> list[TaskMatch]:
    """Отдельные просьбы: собственное намерение в предложении или общее перед «и».

    Не превращаем все упомянутые товары/услуги в ниши. Каждая часть проходит
    обычный matcher; намерение не переносится в следующее предложение.
    """
    primary = match(text, rules, context)
    if primary is None:
        return []
    from app.services import geo

    raw_all = tokens(text)
    lem_all = [_lemma(t) for t in raw_all]
    # Минусы категории относятся ко всему исходному сообщению, как до разбиения.
    # Иначе слово из соседней задачи могло бы обойти исключение владельца.
    rules = Rules(rules.intents, rules.negatives,
                  [n for n in rules.nodes if not negative_hit(lem_all, raw_all, n.negatives)])
    roots = {n.id: n.parent_id or n.id for n in rules.nodes}
    found = {}
    for sentence in re.split(r"[;\n]+|(?<=[.!?])\s+", text):
        raw = tokens(sentence)
        lem = [_lemma(t) for t in raw]
        prefix = ""
        for i in range(len(lem)):
            phrase = next((p for p in rules.intents if lem[i:i + len(p)] == p), None)
            if phrase:
                prefix = " ".join(raw[i:i + len(phrase)])
                break
        for fragment in re.split(r"\s+и\s+|,\s*(?=(?:нуж|треб|ищ|надо))", sentence, flags=re.I):
            if re.match(r"\s*(?:но\s+)?не\s+(?:нуж|треб|надо|ищ)", fragment, flags=re.I):
                continue
            common = geo.request_places(text)
            fragment_context = context + (" " + common[0][0] if len(common) == 1 else "")
            m = match(fragment, rules, fragment_context)
            available = re.search(r"\bуже\s+(?:есть|приш|найд|наня|готов)", fragment, flags=re.I)
            if m is None and prefix and not available:
                m = match(prefix + " " + fragment, rules, fragment_context)
            if m is None:
                continue
            place_text = fragment
            if not geo.request_places(fragment):
                if len(geo.request_places(sentence)) == 1:
                    place_text = sentence
                elif len(geo.request_places(text)) == 1:
                    place_text = text
            found.setdefault(roots[m.category_id], TaskMatch(m, fragment, place_text))
    if len(found) < 2:
        if len(found) == 1:
            task = next(iter(found.values()))
            if roots[primary.category_id] in found:
                task.match = primary
            return [task]
        return [TaskMatch(primary, text, text)]
    root = roots[primary.category_id]
    first = found.pop(root, None)
    if first is not None:
        first.match = primary
    return ([first] if first else []) + list(found.values())[:6 - bool(first)]
