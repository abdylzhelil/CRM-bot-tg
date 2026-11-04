"""Простой черновик ответа по правилам (без ИИ)."""
import random

import pytest

from app.services import draft


@pytest.mark.parametrize("text,expected", [
    ("Ищу сайт, бюджет 30к", "30 тыс. ₽"), ("готов отдать до 50 000 руб", "50000 ₽"),
    ("нужно за 15 тыс", "15 тыс. ₽"), ("нужен сайт", ""), ("через 2 дня", ""),
])
def test_budget(text, expected):
    assert draft.find_budget(text) == expected


def test_default_draft_has_topic_prices_question_and_context():
    text = draft.build("Срочно нужен сайт, бюджет 30к", "Али Магомедов", "Сайты", "", "лендинг от 15 000 ₽", random.Random(1))
    assert "Али" in text and "по сайту" in text
    assert "срочно" in text.lower() and "30 тыс. ₽" in text and "лендинг от 15 000" in text
    assert any(q in text for q in draft.QUESTIONS["Сайты"])


def test_users_template_is_kept_and_extended():
    text = draft.build("Нужен дизайнер", "Ася", "Дизайн", "Здравствуйте{, имя}! Я делаю логотипы. {цены}", "логотип 3000 ₽", random.Random(2))
    assert text.startswith("Здравствуйте, Ася! Я делаю логотипы.") and "Цены: логотип 3000 ₽" in text
    assert any(q in text for q in draft.QUESTIONS["Дизайн"])


def test_variants_differ_and_unknown_niche_ok():
    seen = {draft.build("Нужна помощь", "", "Что-то новое", "", "", random.Random(i)) for i in range(20)}
    assert len(seen) > 1  # «Другой вариант» действительно даёт другой текст
    assert all("по вашей задаче" in t for t in seen) and all(not t.startswith(" ") for t in seen)
