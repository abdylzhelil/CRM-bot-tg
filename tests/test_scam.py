"""«Реальное объявление или схема?» — предупреждение в карточке и кнопка «🆘 Сообщить о проблеме»."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from app.services import scam


def test_scam_signs():
    assert "просят предоплату" in scam.signs("Нужен сантехник, предоплата 50%, пишите в лс")
    assert "оплата в криптовалюте" in scam.signs("Ищу дизайнера, оплата в USDT")
    assert "просят код из СМС, данные карты или паспорта" in scam.signs("скиньте код из смс для регистрации")
    assert "обещают крупную оплату в день или час" in scam.signs("Нужны люди, 5000 ₽ в день, простые задания")
    assert scam.signs("Нужен сайт для салона, бюджет 60000₽") == []
    assert scam.warning("Ищу ремонт кухни") == ""


def test_card_shows_warning_and_button():
    from app.bot import notify

    lead = SimpleNamespace(suspicious=False, text="Ищу курьера, оплата в крипте, пишите https://bit.ly/x", name="А",
                           username="", group_title="Г", link="", city="", region="", spam_score=0, gone_at=None,
                           author_id=5)
    text = notify.lead_text(lead, "Курьеры")
    assert "Осторожно, возможна схема" in text and "криптовалюте" in text
    labels = [b.text for row in notify.lead_keyboard(lead, 1).inline_keyboard for b in row]
    assert "🚫 Спам / схема" in labels


async def test_problem_report_reaches_owner(maker, tgbot):
    from app.bot.handlers import feedback
    from app.models import User

    async with maker() as s:
        s.add(User(tg_id=8080, name="Юзер", onboarded=True))
        await s.commit()
        m = MagicMock()
        m.from_user.id = 8080
        m.answer = AsyncMock()
        await feedback.on_input(s, m, {"action": "feedback", "problem": True}, "нажал Тариф — не открылся")
    assert any(t.startswith("💬") and "🆘 Проблема в боте" in t for _, t in tgbot.sent)
