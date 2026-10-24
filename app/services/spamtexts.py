"""Спам-тексты: спамеры пишут одно и то же с разных аккаунтов и слегка меняют слова.

Чёрный список работает по автору, а это — по тексту: если сообщение похоже на уже отмеченный спам
(общие «основы» слов), заявкой оно не станет, от какого бы аккаунта ни пришло.
"""
import re
import time

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import SpamSample

MIN_WORDS = 5  # короткие тексты («нужен сайт») не сравниваем — слишком легко ошибиться
JACCARD = 0.6  # доля общих слов от всех слов двух текстов
CONTAINS = 0.85  # или почти все слова короткого текста есть в другом (спамер дописал строчку)
_WORD = re.compile(r"[a-zа-яё]{3,}")
_TTL = 60
_cache: tuple[float, list[tuple[int, frozenset[str]]]] | None = None


def words(text: str) -> frozenset[str]:
    """Основы слов: первые 5 букв — так «заработок/заработать/заработком» считаются одним словом."""
    return frozenset(w[:5] for w in _WORD.findall(text.lower().replace("ё", "е")))


def similar(a: frozenset[str], b: frozenset[str]) -> bool:
    if len(a) < MIN_WORDS or len(b) < MIN_WORDS:
        return False
    common = len(a & b)
    return common / len(a | b) >= JACCARD or common / min(len(a), len(b)) >= CONTAINS


def invalidate() -> None:
    global _cache
    _cache = None


async def _samples(s: AsyncSession) -> list[tuple[int, frozenset[str]]]:
    global _cache
    if _cache and time.monotonic() - _cache[0] < _TTL:
        return _cache[1]
    rows = (await s.execute(select(SpamSample.id, SpamSample.text))).all()
    _cache = (time.monotonic(), [(i, words(t)) for i, t in rows])
    return _cache[1]


async def match(s: AsyncSession, text: str) -> int | None:
    """id похожего образца или None."""
    w = words(text)
    if len(w) < MIN_WORDS:
        return None
    for sample_id, sw in await _samples(s):
        if similar(w, sw):
            return sample_id
    return None


async def remember(s: AsyncSession, text: str, lead_id: int = 0, added_by: int = 0) -> SpamSample | None:
    """Запомнить спам-текст (если похожего ещё нет и текст не слишком короткий). Коммит — на вызывающем."""
    if len(words(text)) < MIN_WORDS or await match(s, text):
        return None
    sample = SpamSample(text=text[:1000], lead_id=lead_id, added_by=added_by)
    s.add(sample)
    await s.flush()
    invalidate()
    return sample


async def forget_lead(s: AsyncSession, lead_id: int) -> None:
    """Заявку вернули из «спама» — её образец тоже убираем."""
    for row in (await s.scalars(select(SpamSample).where(SpamSample.lead_id == lead_id))).all():
        await s.delete(row)
    invalidate()


async def count(s: AsyncSession) -> int:
    return await s.scalar(select(func.count()).select_from(SpamSample)) or 0
