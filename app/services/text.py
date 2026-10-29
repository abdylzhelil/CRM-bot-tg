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


def compile_phrases(words: list[str]) -> list[list[str]]:
    return [p for p in (lemmas(w) for w in words) if p]


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

    best: tuple[int, int, Match] | None = None
    for node in rules.nodes:
        if negative_hit(lem, raw, node.negatives):
            continue
        if node.city and not (has_phrase(lem, node.city) or has_phrase(ctx, node.city)):
            continue
        words = [p for p in node.words if has_phrase(lem, p)]
        strong = [p for p in node.strong if has_phrase(lem, p)]
        if not strong and not (has_intent and words):
            continue
        score = len(words) + 2 * len(strong)
        key = (score, node.depth)
        if best is None or key > best[:2]:
            best = (*key, Match(node.id, node.name, [" ".join(p) for p in words + strong]))
    return best[2] if best else None
