"""Мини-приложение: проверка подписи Telegram, права по базе, ответ /api/me."""
import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest
from aiohttp.test_utils import TestClient, TestServer

from app import config
from app.web import server as web_server
from app.web.auth import verify_init_data
from tests.conftest import ADMIN_ID, make_user

TOKEN = config.BOT_TOKEN


def sign(user: dict, token: str = TOKEN, auth_date: int | None = None) -> str:
    fields = {"auth_date": str(auth_date if auth_date is not None else int(time.time())), "query_id": "AAA", "user": json.dumps(user)}
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def test_verify_init_data():
    good = sign({"id": 7, "first_name": "A"})
    assert verify_init_data(good, TOKEN)["id"] == 7
    assert verify_init_data(good, "999:OTHER") is None  # чужой токен
    assert verify_init_data(good.replace("%22id%22%3A+7", "%22id%22%3A+8"), TOKEN) is None  # подмена пользователя
    assert verify_init_data(sign({"id": 7}, auth_date=int(time.time()) - 90000), TOKEN) is None  # просрочено
    assert verify_init_data("", TOKEN) is None and verify_init_data("hash=abc", TOKEN) is None
    assert verify_init_data("мусор", TOKEN) is None


@pytest.fixture
async def client(maker):
    web_server._hits.clear()
    c = TestClient(TestServer(web_server.make_app()))
    await c.start_server()
    yield c
    await c.close()


def tma(user_id: int, **kw) -> dict:
    return {"Authorization": "tma " + sign({"id": user_id, "first_name": "T"}, **kw)}


async def test_api_requires_valid_signature(client):
    assert (await client.get("/api/me")).status == 401
    assert (await client.get("/api/me", headers={"Authorization": "tma подделка"})).status == 401
    r = await client.get("/api/me", headers=tma(5, token="1:BAD"))
    assert r.status == 401


async def test_unregistered_user_is_refused(client, maker):
    r = await client.get("/api/me", headers=tma(404))
    assert r.status == 403 and (await r.json())["error"] == "not_registered"


async def test_me_for_free_paid_and_admin(client, maker):
    await make_user(maker, 10)
    await make_user(maker, 11, plan="full")
    from app.models import User

    async with maker() as s:
        if await s.get(User, ADMIN_ID) is None:
            s.add(User(tg_id=ADMIN_ID, is_admin=True, onboarded=True, plan="full"))
            await s.commit()
    free = await (await client.get("/api/me", headers=tma(10))).json()
    assert free["role"] == "free" and free["plan"]["free_limit"] == 5 and [s["id"] for s in free["sections"]] == ["home", "leads", "clients", "profile"]
    paid = await (await client.get("/api/me", headers=tma(11))).json()
    assert paid["role"] == "paid" and paid["plan"]["free_limit"] is None
    adm = await (await client.get("/api/me", headers=tma(ADMIN_ID))).json()
    assert adm["role"] == "admin" and "admin" in [s["id"] for s in adm["sections"]]


async def test_role_comes_from_database_not_client(client, maker):
    await make_user(maker, 12)
    assert (await (await client.get("/api/me", headers=tma(12))).json())["role"] == "free"
    async with maker() as s:
        from app.models import User

        (await s.get(User, 12)).blocked = True
        await s.commit()
    assert (await client.get("/api/me", headers=tma(12))).status == 403


async def test_rate_limit_and_static(client, maker, monkeypatch):
    await make_user(maker, 13)
    monkeypatch.setattr(web_server, "RATE_LIMIT", 3)
    codes = [(await client.get("/api/me", headers=tma(13))).status for _ in range(5)]
    assert codes == [200, 200, 200, 429, 429]
    page = await client.get("/app/")
    assert page.status == 200 and "КлиентРадар" in await page.text()
    assert "default-src 'self'" in page.headers["Content-Security-Policy"]
    assert (await client.get("/app/app.js")).status == 200
    assert (await client.get("/healthz")).status == 200


def test_kb_builds_webapp_button():
    from app.bot.common import kb

    btn = kb([[("📱 Кабинет", "webapp:https://x.sslip.io/app/")]]).inline_keyboard[0][0]
    assert btn.web_app.url == "https://x.sslip.io/app/" and btn.callback_data is None
