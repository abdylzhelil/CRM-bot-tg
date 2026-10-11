"""Отбор найденных групп списком, общая база ботов, пауза разведчика."""
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy import select

from app.bot.handlers import groups
from app.models import FeedChat, Group, ScoutAccount
from app.scout import joiner
from app.scout.search import is_covered


def cb(data):
    c = MagicMock()
    c.data = data
    c.answer, c.message.edit_text, c.message.answer = AsyncMock(), AsyncMock(), AsyncMock()
    return c


async def add_groups(maker, n, status="candidate"):
    async with maker() as s:
        for i in range(n):
            s.add(Group(tg_id=-1000 - i, title=f"Группа {i}", members=1000 - i, status=status))
        await s.commit()


async def statuses(maker):
    async with maker() as s:
        return [g.status for g in (await s.scalars(select(Group).order_by(Group.id))).all()]


async def test_review_one_and_all(maker):
    await add_groups(maker, 10)
    c = cb("rv:page")
    await groups.review_page(c)
    text = c.answer.call_args.args[0] if c.answer.call_args and c.answer.call_args.args else ""
    async with maker() as s:
        first = await s.scalar(select(Group).order_by(Group.members.desc()))
    await groups.review_one(cb(f"rv:j:{first.id}"))
    await groups.review_all(cb("rv:all:s"))
    st = await statuses(maker)
    assert st.count("approved") == 1 and st.count("skipped") == 8 and st.count("candidate") == 1
    assert text is not None


async def test_shared_base_scout_does_not_join_where_our_bot_is(maker):
    async with maker() as s:
        s.add(FeedChat(tg_id=-555, title="Группа с ботом модерации"))
        s.add(Group(tg_id=-555, title="Группа с ботом модерации", status="approved"))
        s.add(ScoutAccount(id=1, name="scout1", api_id=1, api_hash="x", daily_join_limit=10))
        await s.commit()
        assert await is_covered(s, -555)
    client = AsyncMock()
    assert await joiner.join_one(client, 1) is False
    client.assert_not_called()
    assert await statuses(maker) == ["covered"]


async def test_pause_toggle(maker):
    from app.services import settings

    assert not await joiner.paused()
    c = cb("gr:pause")
    c.bot.me = AsyncMock(return_value=MagicMock(can_read_all_group_messages=True, username="b"))
    await groups.toggle_pause(c)
    assert await joiner.paused()
    async with maker() as s:
        assert await settings.get(s, "scout_paused") == "1"


async def test_scout_joins_unassigned_approved_group(maker, monkeypatch):
    from app.scout import folders

    tidy = AsyncMock(return_value=1)
    monkeypatch.setattr(folders, "sync_folders", tidy)
    monkeypatch.setattr(folders, "mute", AsyncMock())
    async with maker() as s:
        s.add(Group(tg_id=-777, username="mhk_biz", title="Бизнес МХК", status="approved"))
        s.add(ScoutAccount(id=2, name="scout2", api_id=1, api_hash="x", daily_join_limit=10))
        await s.commit()
    client = AsyncMock()
    client.get_entity = AsyncMock(return_value=object())
    assert await joiner.join_one(client, 2) is True
    async with maker() as s:
        g = await s.scalar(select(Group).where(Group.tg_id == -777))
        acc = await s.get(ScoutAccount, 2)
    assert g.status == "joined" and g.account_id == 2 and acc.joined_today == 1
    tidy.assert_awaited_once()  # после вступления группа попадает в папку


class FakeDialogs:
    def __init__(self, ids):
        from types import SimpleNamespace

        from telethon.tl.types import Channel, InputPeerChannel

        self.calls = []
        self.dialogs = [
            SimpleNamespace(
                entity=Channel(id=abs(i) - 1000000000000, title="g", photo=None, date=None, megagroup=True),
                input_entity=InputPeerChannel(channel_id=abs(i) - 1000000000000, access_hash=1),
            )
            for i in ids
        ]

    async def iter_dialogs(self):
        for d in self.dialogs:
            yield d

    async def __call__(self, request):
        self.calls.append(request)


async def test_scout_groups_go_to_folders(maker):
    from app.scout.folders import FIRST_FOLDER_ID, sync_folders

    ids = [-1000000000000 - n for n in range(1, 151)]  # 150 групп → 2 папки
    async with maker() as s:
        for i in ids:
            s.add(Group(tg_id=i, title="g", status="joined", account_id=3))
        await s.commit()
    client = FakeDialogs(ids)
    assert await sync_folders(client, 3) == 150
    made = [c for c in client.calls if c.filter is not None]
    removed = [c for c in client.calls if c.filter is None]
    assert [c.id for c in made] == [FIRST_FOLDER_ID, FIRST_FOLDER_ID + 1]
    assert len(made[0].filter.include_peers) == 100 and len(made[1].filter.include_peers) == 50
    assert made[0].filter.title.text == "🔍 Разведка" and made[1].filter.title.text == "🔍 Разведка 2"
    assert len(removed) == 3  # лишние папки 12–14 удаляются
