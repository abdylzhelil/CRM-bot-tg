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


def lemmas(text: str) -> list[str]:
    tokens = _TOKEN.findall(text.lower().replace("ё", "е"))
    return [_lemma(t) for t in tokens]


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
    negatives: list[list[str]] = field(default_factory=list)


@dataclass
class Rules:
    intents: list[list[str]] = field(default_factory=list)
    negatives: list[list[str]] = field(default_factory=list)
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
    lem = lemmas(text)
    if not lem:
        return None
    if any(has_phrase(lem, p) for p in rules.negatives):
        return None
    has_intent = any(has_phrase(lem, p) for p in rules.intents)
    ctx = lemmas(context) if context else []

    best: tuple[int, int, Match] | None = None
    for node in rules.nodes:
        if any(has_phrase(lem, p) for p in node.negatives):
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
