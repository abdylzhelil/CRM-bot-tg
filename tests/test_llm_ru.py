"""Российские ИИ (GigaChat / YandexGPT / своя модель) в CRM: выбор, разбор ответа, помощник и черновики."""
import pytest

from app import config
from app.models import AiCall
from app.services import ai, brain, llm, offers, settings
from tests.conftest import make_user


def test_pick_order_and_choice(monkeypatch):
    for k in ("GIGACHAT_AUTH_KEY", "YANDEX_API_KEY", "YANDEX_FOLDER_ID", "ANTHROPIC_API_KEY", "AI_PROVIDER"):
        monkeypatch.setattr(config, k, "")
    monkeypatch.setattr(config, "LOCAL_AI", False)
    assert llm.pick() == "" and not ai.enabled()
    monkeypatch.setattr(config, "YANDEX_API_KEY", "k")
    monkeypatch.setattr(config, "YANDEX_FOLDER_ID", "f")
    monkeypatch.setattr(config, "GIGACHAT_AUTH_KEY", "g")
    assert llm.pick("auto") == "gigachat"          # по порядку: GigaChat первым
    assert llm.pick("yandex") == "yandex"           # выбор в ⚙️
    assert llm.pick("anthropic") == "gigachat"      # выбранного ключа нет — берём доступный
    monkeypatch.setattr(config, "AI_PROVIDER", "yandex")
    assert llm.pick("auto") == "yandex"             # как в .env бота модерации


def test_json_helpers():
    schema = brain.obj({"kind": {"type": "string", "enum": ["ЗАКАЗЧИК", "ИСПОЛНИТЕЛЬ", "ДРУГОЕ"]}, "ok": {"type": "boolean"},
                        "ids": {"type": "array", "items": {"type": "integer"}}})
    hint = llm.schema_hint(schema)
    assert '"kind": "ЗАКАЗЧИК" или' in hint and '"ok": true или false' in hint
    raw = 'Вот ответ:\n```json\n{"kind": "ИСПОЛНИТЕЛЬ", "ok": "true", "ids": [1, 2], "лишнее": 1}\n```'
    assert llm.fits(llm.extract_json(raw), schema) == {"kind": "ИСПОЛНИТЕЛЬ", "ok": True, "ids": [1, 2]}
    assert llm.fits({"kind": "ХЗ", "ok": True, "ids": []}, schema) is None  # не из списка — не верим
    assert llm.fits(llm.extract_json("нет json"), schema) is None


async def test_brain_and_filter_use_gigachat(maker, monkeypatch):
    monkeypatch.setattr(config, "GIGACHAT_AUTH_KEY", "g")
    calls = []

    async def fake_complete(provider, system, text, max_tokens=400, json_mode=False):
        calls.append((provider, system, text))
        return '{"kind": "ИСПОЛНИТЕЛЬ"}'
    monkeypatch.setattr(llm, "complete", fake_complete)
    async with maker() as s:
        assert await offers.ai_verdict(s, "Помогу с сайтом, недорого") == "offer"
        assert calls[0][0] == "gigachat" and "Ответь ТОЛЬКО JSON" in calls[0][1] and "<данные>" in calls[0][2]
        assert await s.scalar(__import__("sqlalchemy").select(AiCall.model)) == "gigachat"
        # ИИ ответил ерундой — работаем по правилам (None), а не падаем

        async def junk(*a, **k):
            return "не знаю"
        monkeypatch.setattr(llm, "complete", junk)
        assert await offers.ai_verdict(s, "Ещё одно сообщение") is None

        async def down(*a, **k):
            raise llm.LlmError("gigachat 401")
        monkeypatch.setattr(llm, "complete", down)
        assert await offers.ai_verdict(s, "Третье сообщение") is None
        await settings.put(s, "ai_decide", "0")
        assert await brain.explain_error(s, "оплата", "Refund declined") == ""  # выключено — без запросов


async def test_draft_with_russian_ai(maker, monkeypatch):
    from app.models import Lead, User

    monkeypatch.setattr(config, "GIGACHAT_AUTH_KEY", "g")

    async def fake_complete(provider, system, text, max_tokens=400, json_mode=False):
        return "Здравствуйте! Сделаю сайт за неделю, напишите детали."
    monkeypatch.setattr(llm, "complete", fake_complete)
    await make_user(maker, 95, plan="full")
    async with maker() as s:
        await settings.put(s, "ai_enabled", "1")
        user = await s.get(User, 95)
        lead = Lead(author_id=5, text="Ищу кто сделает сайт", text_hash="x")
        s.add(lead)
        await s.commit()
        text = await ai.draft_reply(s, user, lead, "Сайты", "", "")
    assert text.startswith("Здравствуйте")

    async def down(*a, **k):
        raise llm.LlmError("gigachat timeout")
    monkeypatch.setattr(llm, "complete", down)
    async with maker() as s:
        with pytest.raises(ai.AiError, match="недоступен"):
            await ai.draft_reply(s, await s.get(User, 95), lead, "Сайты", "", "")
