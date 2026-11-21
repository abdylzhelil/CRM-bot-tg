"""«Сообщение не найдено» (25.10): ссылка с активным @адресом группы, проверка удалённых сообщений, фильтр самопрезентаций."""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.models import Group, Lead, ScoutAccount, now
from app.scout.manager import public_username
from app.services import offers


def test_public_username_from_usernames():
    assert public_username(SimpleNamespace(username="bar")) == "bar"
    chat = SimpleNamespace(username=None, usernames=[SimpleNamespace(username="old", active=False),
                                                     SimpleNamespace(username="mkala_bar", active=True)])
    assert public_username(chat) == "mkala_bar"
    assert public_username(SimpleNamespace(username=None, usernames=None)) == ""


def test_seller_self_intro():
    assert offers.rule_verdict("Здравствуйте! Увидела ваше сообщение. Я занимаюсь созданием контента и оформлением "
                               "карточек, собираю портфолио, поэтому готова взяться за небольшую оплату") == "offer"
    assert offers.rule_verdict("Нужен дизайнер карточек, кто готов взяться? бюджет 3000") == "request"


async def test_liveness_marks_deleted(maker, monkeypatch):
    from app.bot import notify
    from app.scout import publisher

    monkeypatch.setattr(publisher, "resolve", AsyncMock(return_value=object()))
    monkeypatch.setattr(publisher.asyncio, "sleep", AsyncMock())
    async with maker() as s:
        s.add(ScoutAccount(id=1, name="sc", api_id=1, api_hash="x"))
        await s.flush()
        g = Group(tg_id=-77, title="Барахолка", status="joined", account_id=1)
        s.add(g)
        await s.flush()
        s.add_all([Lead(author_id=1, text="Нужен сантехник срочно", text_hash="a", group_id=g.id, message_id=10,
                        link="https://t.me/bar/10"),
                   Lead(author_id=2, text="Нужен электрик срочно", text_hash="b", group_id=g.id, message_id=11,
                        link="https://t.me/bar/11")])
        await s.commit()

    class Client:
        async def get_messages(self, entity, ids):
            return [SimpleNamespace(id=10), None]  # 11 — удалено

    assert await publisher.liveness_round(SimpleNamespace(clients={1: Client()})) == 1
    async with maker() as s:
        from sqlalchemy import select

        leads = {ld.message_id: ld for ld in (await s.scalars(select(Lead))).all()}
    assert leads[10].gone_at is None and leads[11].gone_at is not None
    card = notify.lead_text(leads[11], "Электрики")
    assert "удалено" in card and "t.me/bar/11" not in card
    kb = notify.lead_keyboard(leads[11], 1)
    assert not any((b.url or "").startswith("https://t.me/bar") for row in kb.inline_keyboard for b in row)
    # проверенные недавно повторно не проверяются
    assert await publisher.liveness_round(SimpleNamespace(clients={1: Client()})) == 0
