"""📋 Правила групп: разведчик читает правила всех групп, ИИ/слова решают про рекламу, списки и сводка владельцу."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy import select

from app import config
from app.models import Group, ScoutAccount
from app.services import llm, passport, settings


def test_words_and_contact():
    assert passport.policy_from_words("", "") == "unknown"
    assert passport.policy_from_words("", "Барахолка района") == "allowed"
    assert passport.policy_from_words("в правилах группы: «реклама запрещена»", "x") == "forbidden"
    assert passport.policy_from_words("в правилах группы: «реклама только через админа»", "x") == "only_admin"
    assert passport.contact_from_text("По вопросам рекламы пишите @ivan_admin") == "ivan_admin"
    assert passport.contact_from_text("Правила: не спамить") == ""


class Client:
    """Telethon-клиент: описание и закреп группы."""

    def __init__(self, about):
        self.about = about

    async def __call__(self, req):
        return SimpleNamespace(full_chat=SimpleNamespace(about=self.about, slowmode_seconds=0, pinned_msg_id=None))

    async def get_entity(self, _):
        return object()

    async def get_participants(self, *a, **k):
        return []


async def test_round_reads_rules_with_ai_and_sends_digest(maker, tgbot, monkeypatch):
    from app.scout import publisher

    monkeypatch.setattr(config, "GIGACHAT_AUTH_KEY", "g")

    async def fake_complete(provider, system, text, max_tokens=400, json_mode=False):
        return ('{"ads": "only_admin", "subscribe_channels": [], "invite_count": 0, "hours": "", '
                '"summary": "Реклама по согласованию с админом, 500 ₽", "contact": "@boss_ads"}')
    monkeypatch.setattr(llm, "complete", fake_complete)
    monkeypatch.setattr(publisher.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(publisher, "resolve", AsyncMock(return_value=object()))
    async with maker() as s:
        s.add(ScoutAccount(id=1, name="sc", api_id=1, api_hash="x"))
        await s.flush()
        s.add(Group(tg_id=-701, title="Барахолка Москва", status="joined", account_id=1, members=900))
        await s.commit()
    manager = SimpleNamespace(clients={1: Client("Объявления. Реклама — только по согласованию с @boss_ads")})
    assert await publisher.rules_round(manager) == 1
    async with maker() as s:
        g = await s.scalar(select(Group).where(Group.tg_id == -701))
    assert (g.rules_policy, g.rules_contact) == ("only_admin", "boss_ads") and "500" in g.rules_summary
    digest = [t for t in tgbot.texts(1) if "Правила групп" in t]
    assert digest and "Только через админа: 1" in digest[0] and "Написать" in digest[0]
    # повторно сразу не читает (раз в 2 недели) и сводку чаще раза в сутки не шлёт
    assert await publisher.rules_round(manager) == 0
    assert len([t for t in tgbot.texts(1) if "Правила групп" in t]) == 1
    # выключено в ⚙️ — не читает
    async with maker() as s:
        await settings.put(s, "group_rules_check", "0")
        g.rules_checked_at = None
    assert await publisher.rules_round(manager) == 0


async def test_rules_screens(maker, tgbot):
    from app.bot.handlers import groups as gh

    async with maker() as s:
        s.add(Group(tg_id=-702, title="Ремонт чат", status="joined", members=500, rules_policy="paid",
                    rules_summary="Реклама 300 ₽", rules_contact="adm_rem"))
        await s.commit()
    c = MagicMock()
    c.answer, c.message.answer = AsyncMock(), AsyncMock()
    c.data = "gr:rules"
    await gh.rules_menu(c)
    markup = c.answer.call_args.kwargs["reply_markup"]
    assert any("Платно у админа · 1" in b.text for row in markup.inline_keyboard for b in row)
    c2 = MagicMock()
    c2.answer, c2.message.answer = AsyncMock(), AsyncMock()
    c2.data = "gr:rl:paid"
    await gh.rules_list(c2)
    card = c2.message.answer.call_args_list[-1]
    assert "Реклама 300 ₽" in card.args[0] and "@adm_rem" in card.args[0]
    buttons = [b for row in card.kwargs["reply_markup"].inline_keyboard for b in row]
    assert any(b.url == "https://t.me/adm_rem" for b in buttons) and any(b.copy_text for b in buttons)
