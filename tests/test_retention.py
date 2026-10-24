"""Срок хранения заявок и удаление данных автора."""
from datetime import timedelta

from sqlalchemy import select

from app.models import BlacklistEntry, Client, ClientEvent, Delivery, Lead, SpamVote, now
from app.services import retention, settings
from tests.conftest import make_user


async def add_lead(maker, user_id, author=900, age_days=0, status="new", username="client_u", text="Нужен сайт"):
    async with maker() as s:
        lead = Lead(author_id=author, username=username, name="Клиент", group_title="Г", text=text,
                    text_hash=f"h{author}{age_days}{text}"[:40], created_at=now() - timedelta(days=age_days))
        s.add(lead)
        await s.flush()
        s.add(Delivery(lead_id=lead.id, user_id=user_id, status=status, sent=True))
        await s.commit()
        return lead.id


async def lead_ids(maker):
    async with maker() as s:
        return set((await s.scalars(select(Lead.id))).all())


async def test_purge_old_keeps_fresh_and_taken(maker):
    await make_user(maker, 70)
    old = await add_lead(maker, 70, author=1, age_days=100)
    old_taken = await add_lead(maker, 70, author=2, age_days=100, status="taken")
    fresh = await add_lead(maker, 70, author=3, age_days=10)
    edge = await add_lead(maker, 70, author=4, age_days=89)
    async with maker() as s:
        assert await retention.purge_old(s, 0) == 0  # 0 — не удалять
    assert await lead_ids(maker) == {old, old_taken, fresh, edge}
    async with maker() as s:
        assert await retention.purge_old(s, 90) == 1
    assert await lead_ids(maker) == {old_taken, fresh, edge}  # взятая в работу, свежие и «89 дней» остаются
    async with maker() as s:
        assert not (await s.scalars(select(Delivery).where(Delivery.lead_id == old))).all()  # доставка тоже удалена
        assert await retention.purge_old(s, 90) == 0  # повторный запуск ничего не ломает


async def test_purge_handles_many_leads(maker):
    await make_user(maker, 71)
    async with maker() as s:
        for i in range(1200):
            lead = Lead(author_id=i, text="x", text_hash=f"m{i}", created_at=now() - timedelta(days=200))
            s.add(lead)
        await s.commit()
    async with maker() as s:
        assert await retention.purge_old(s, 90) == 1200
    assert await lead_ids(maker) == set()


async def test_forget_author_removes_everything_and_anonymizes_crm(maker):
    await make_user(maker, 72)
    await make_user(maker, 73)
    mine = [await add_lead(maker, 72, author=900, text=f"Заявка {i}") for i in range(3)]
    await add_lead(maker, 72, author=901, text="чужая")
    async with maker() as s:
        s.add(SpamVote(author_id=900, user_id=72))
        s.add(SpamVote(author_id=901, user_id=72))
        s.add(BlacklistEntry(user_id=900, username="client_u", status="spam", reason="тест"))
        c = Client(user_id=72, tg_id=900, name="Марат", username="client_u", phone="+7900", source="Г", notes="звонил", budget=30000, stage="talk")
        other = Client(user_id=73, tg_id=901, name="Оля", username="olya")
        s.add_all([c, other])
        await s.flush()
        s.add(ClientEvent(client_id=c.id, text="Заявка: Нужен сайт"))
        await s.commit()
        card_id = c.id
    async with maker() as s:
        st = await retention.forget_author(s, 900)
    assert st["leads"] == 3 and st["deliveries"] == 3 and st["crm"] == 1 and st["blacklist"] is True
    ids = await lead_ids(maker)
    assert not (ids & set(mine)) and len(ids) == 1  # чужая заявка осталась
    async with maker() as s:
        assert await s.get(BlacklistEntry, 900) is None
        assert (await s.scalars(select(SpamVote).where(SpamVote.author_id == 900))).all() == []
        assert len((await s.scalars(select(SpamVote).where(SpamVote.author_id == 901))).all()) == 1
        card = await s.get(Client, card_id)
        assert (card.name, card.username, card.phone, card.notes, card.tg_id) == ("Удалён по просьбе", "", "", "", 0)
        assert card.budget == 30000 and card.stage == "talk"  # у пользователя остаются цифры сделки
        assert (await s.scalars(select(ClientEvent).where(ClientEvent.client_id == card_id))).all() == []
        assert (await s.get(Client, (await s.scalar(select(Client.id).where(Client.username == "olya"))))).name == "Оля"


async def test_find_authors_by_id_and_username(maker):
    await make_user(maker, 74)
    await add_lead(maker, 74, author=905, username="Spammer_X")
    async with maker() as s:
        assert await retention.find_authors(s, "905") == [(905, "Spammer_X")]
        assert [a for a, _ in await retention.find_authors(s, "@spammer_x")] == [905]  # без учёта регистра
        assert await retention.find_authors(s, "@нет_такого") == []
        assert await retention.find_authors(s, "  ") == []


def test_lead_keep_days_setting():
    assert settings.DEFAULTS["lead_keep_days"] == "90" and settings.validate("lead_keep_days", "0") == "0"
    assert settings.validate("lead_keep_days", "365") == "365"
    for bad in ("-1", "много", "99999"):
        try:
            settings.validate("lead_keep_days", bad)
        except settings.SettingError:
            continue
        raise AssertionError(bad)
