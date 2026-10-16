"""Заказчик или исполнитель: реклама услуг не приходит как заявка."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy import func, select

from app import config
from app.models import AiCall, FilteredMessage, Lead
from app.services import intake, offers, settings
from app.services.antispam import AuthorInfo
from app.services.pipeline import Incoming
from tests.conftest import ADMIN_ID, make_user

OFFER = ("🚀 Нужен сайт именно под ваш бизнес? Сделаю за 3 дня, от 14000 ₽\n\nЧем занимаюсь:\n🌐 сайты и лендинги\n"
         "🤖 Telegram-боты\n📊 CRM под ваши процессы\n\n📂 Примеры работ скину в лс")
REQUEST = "Ищу кто сделает сайт для салона красоты, бюджет до 40 тысяч, подскажите хорошего разработчика"


def msg(text, author=9100):
    return Incoming(text=text, author=AuthorInfo(user_id=author, username="u", first_name="Иван"),
                    group_tg_id=-100900, group_title="Бизнес | Предприниматели РФ", message_id=author)


def test_rules():
    assert offers.rule_verdict(OFFER) == "offer"
    assert offers.rule_verdict(REQUEST) == "request"
    assert offers.rule_verdict("Нужен лендинг для кофейни, кто возьмётся?") == "request"
    assert offers.rule_verdict("Делаю сайты. Кто знает, где найти клиентов?") == "unsure"


async def test_offer_filtered_and_restorable(maker, tgbot):
    await make_user(maker, 60, plan="full")
    assert await intake.handle(msg(OFFER)) is None
    assert await intake.handle(msg(REQUEST, 9101)) is not None
    async with maker() as s:
        f = await s.scalar(select(FilteredMessage))
        assert f is not None and "реклама услуг" in f.reason
    # админ: «↩️ Это заявка» — сообщение становится заявкой и раздаётся
    from app.bot.handlers import blacklist

    c = MagicMock()
    c.data, c.from_user.id = f"bl:fok:{f.id}", ADMIN_ID
    c.answer, c.message.edit_text, c.message.answer = AsyncMock(), AsyncMock(), AsyncMock()
    await blacklist.filtered_restore(c)
    async with maker() as s:
        assert await s.scalar(select(func.count()).select_from(Lead)) == 2
        assert await s.get(FilteredMessage, f.id) is None


async def test_ai_decides_unsure(maker, tgbot, monkeypatch):
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "k")
    client = MagicMock()
    client.messages.create = AsyncMock(return_value=SimpleNamespace(
        stop_reason="end_turn", content=[SimpleNamespace(type="text", text="ИСПОЛНИТЕЛЬ")],
        usage=SimpleNamespace(input_tokens=200, output_tokens=3)))
    async with maker() as s:
        assert await offers.verdict(s, "Делаю сайты. Кто знает, где найти клиентов?", client) == ("offer", "ИИ")
        await s.commit()
        assert await s.scalar(select(func.count()).select_from(AiCall)) == 1
        kw = client.messages.create.call_args.kwargs
        assert kw["model"] == "claude-haiku-5-5" and "Иван" not in str(kw["messages"])
        await settings.put(s, "ai_classify", "0")  # выключили — ИИ не спрашиваем, сообщение остаётся заявкой
        assert await offers.verdict(s, "Делаю сайты. Кто знает, где найти клиентов?", client) == ("request", "правила")
    assert client.messages.create.await_count == 1


async def test_ai_error_keeps_lead(maker, monkeypatch):
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "k")
    client = MagicMock()
    client.messages.create = AsyncMock(side_effect=RuntimeError("down"))
    async with maker() as s:
        assert (await offers.verdict(s, "Делаю сайты. Кто знает, где найти клиентов?", client))[0] == "request"
