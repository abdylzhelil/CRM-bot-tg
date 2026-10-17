"""ИИ-помощник (Haiku 5.5) в спорных местах: правила групп, проверка рекламы, подбор групп, объяснение ошибок."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy import func, select

from app import config
from app.models import AdOrder, AiCall, Group, ScoutAccount
from app.services import adposts, brain, settings
from tests.conftest import ADMIN_ID, make_user


def fake(answer: dict):
    client = MagicMock()
    client.messages.create = AsyncMock(return_value=SimpleNamespace(
        stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps(answer, ensure_ascii=False))],
        usage=SimpleNamespace(input_tokens=300, output_tokens=40)))
    return client


async def test_no_key_no_calls(maker, monkeypatch):
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "")
    client = fake({"ok": True, "problems": []})
    async with maker() as s:
        assert await brain.check_ad(s, "текст", client) is None
    assert client.messages.create.await_count == 0


async def test_ask_uses_haiku_schema_and_budget(maker, monkeypatch):
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "k")
    client = fake({"ok": False, "problems": ["казино"]})
    async with maker() as s:
        res = await brain.check_ad(s, "Лучшее казино, ставки онлайн", client)
        assert res == {"ok": False, "problems": ["казино"]}
        kw = client.messages.create.call_args.kwargs
        assert kw["model"] == "claude-haiku-5-5" and kw["output_config"]["format"]["type"] == "json_schema"
        assert (await s.scalar(select(AiCall))).purpose == "ad_text"
        await settings.put(s, "ai_classify_daily", "1")  # лимит исчерпан — дальше по правилам
        assert await brain.check_ad(s, "ещё", client) is None
    assert client.messages.create.await_count == 1


async def test_ad_with_problems_goes_to_admin_even_with_auto_approve(maker, tgbot, monkeypatch):
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "k")
    monkeypatch.setattr(brain, "check_ad", AsyncMock(return_value={"ok": False, "problems": ["микрозаймы"]}))
    await make_user(maker, 70)
    async with maker() as s:
        await settings.put(s, "adpost_enabled", "1")
        await settings.put(s, "adpost_auto_approve", "1")
        order = AdOrder(user_id=70, text="Займы до зарплаты без отказа, звоните", status="pending")
        s.add(order)
        await s.commit()
        ok = await adposts.ai_check(s, order)
        assert ok is False and "микрозаймы" in order.ai_check
        assert await adposts.maybe_auto_approve(s, order, ok) is None  # к админу, а не на оплату


async def test_pick_groups_only_from_list(maker, monkeypatch):
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "k")
    async with maker() as s:
        s.add_all([Group(id=11, tg_id=-11, title="Махачкала барахолка", members=5000),
                   Group(id=12, tg_id=-12, title="Москва бизнес", members=900)])
        await s.commit()
        groups = list((await s.scalars(select(Group))).all())
        res = await brain.pick_groups(s, "Ремонт квартир в Махачкале", groups, 300, fake({"ids": [11, 999], "why": "город совпадает"}))
        assert res["ids"] == [11]  # чужих id не бывает


async def test_group_rules_by_ai_overrides_words(maker, tgbot, monkeypatch):
    """В закрепе хитрая формулировка — ИИ понимает, что реклама только через админа, и что нужна подписка."""
    from app.scout import publisher

    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "k")
    monkeypatch.setattr(brain, "group_rules", AsyncMock(return_value={
        "ads": "only_admin", "subscribe_channels": ["dag_news"], "invite_count": 0, "hours": "9-21",
        "summary": "Объявления — только после согласования с @boss"}))
    async with maker() as s:
        s.add(ScoutAccount(id=1, name="sc", api_id=1, api_hash="x", enabled=True, can_post=True, status="ok"))
        s.add(Group(id=5, tg_id=-5, username="g", title="Г", status="joined", kind="group", account_id=1, ads_allowed=True))
        await s.commit()
    client = AsyncMock()
    client.get_entity = AsyncMock(side_effect=[SimpleNamespace(left=False, broadcast=False, creator=False, admin_rights=None,
                                                               banned_rights=None, default_banned_rights=None),
                                               SimpleNamespace(broadcast=True, megagroup=False)])
    client.return_value = SimpleNamespace(full_chat=SimpleNamespace(about="Чат города", pinned_msg_id=1, slowmode_seconds=0))
    client.get_messages = AsyncMock(return_value=SimpleNamespace(message="Пишите объявления, но сначала к старшему"))
    client.get_participants = AsyncMock(return_value=[])
    mgr = SimpleNamespace(clients={1: client})
    level, text = await publisher.check_group(mgr, 5)
    assert level == "bad" and "через админа" in text
    async with maker() as s:
        g = await s.get(Group, 5)
        assert g.ad_hours == "9-21" and "🤖" in g.ad_rules and "dag_news" in g.ad_rules  # подписался по правилам


async def test_explain_error(maker, monkeypatch):
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "k")
    async with maker() as s:
        text = await brain.explain_error(s, "возврат", "Refund declined: the store has a restriction on refunds",
                                         fake({"what": "Магазину запрещены возвраты через API.", "todo": "Напишите в поддержку ЮKassa."}))
        assert text.startswith("🤖") and "поддержку" in text
        assert await s.scalar(select(func.count()).select_from(AiCall)) == 1
