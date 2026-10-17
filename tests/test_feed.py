"""Приём от бота модерации: авторизация, список групп, сообщения только из включённых групп."""
import pytest
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import select

from app import config
from app.feed import server
from app.models import FeedChat, Lead
from tests.conftest import ADMIN_ID

TOKEN = "secret-token"
H = {"Authorization": f"Bearer {TOKEN}"}
CHAT = {"id": -1005550001, "title": "Бизнес Дагестан", "username": "biz_dag"}


@pytest.fixture
async def env(maker, tgbot, monkeypatch):
    monkeypatch.setattr(config, "FEED_TOKEN", TOKEN)
    client = TestClient(TestServer(server.make_app()))
    await client.start_server()
    yield client, maker, tgbot
    await client.close()


def msg(text="Ищу кто сделает интернет-магазин, срочно"):
    return {"chat": CHAT, "message_id": 77, "text": text,
            "user": {"id": 501, "username": "client1", "first_name": "Али", "is_bot": False}}


async def test_rejects_wrong_token(env):
    client, *_ = env
    r = await client.post("/feed/chats", json={"chats": []}, headers={"Authorization": "Bearer nope"})
    assert r.status == 401


async def test_sync_then_message_only_when_enabled(env):
    client, maker, sent = env
    r = await client.post("/feed/chats", json={"chats": [dict(CHAT, own=True)]}, headers=H)
    assert (await r.json())["enabled"] == []
    assert "подключён к CRM" in sent.texts(ADMIN_ID)[-1]

    r = await client.post("/feed/message", json=msg(), headers=H)
    assert (await r.json())["skipped"] == "disabled"

    async with maker() as s:
        (await s.get(FeedChat, CHAT["id"])).enabled = True
        await s.commit()
    r = await client.post("/feed/chats", json={"chats": [dict(CHAT, own=True)]}, headers=H)
    assert (await r.json())["enabled"] == [CHAT["id"]]

    r = await client.post("/feed/message", json=msg(), headers=H)
    assert (await r.json())["lead"]
    async with maker() as s:
        lead = await s.scalar(select(Lead))
    assert lead.link == "https://t.me/biz_dag/77" and lead.group_title == "Бизнес Дагестан"
    assert any("Суть" in t for t in sent.texts(ADMIN_ID))


async def test_new_group_after_first_sync_notifies(env):
    client, _, sent = env
    await client.post("/feed/chats", json={"chats": [dict(CHAT, own=True)]}, headers=H)
    other = {"id": -1005550002, "title": "Аренда МХК", "own": False}
    await client.post("/feed/chats", json={"chats": [dict(CHAT, own=True), other]}, headers=H)
    assert "Аренда МХК" in sent.texts(ADMIN_ID)[-1]
