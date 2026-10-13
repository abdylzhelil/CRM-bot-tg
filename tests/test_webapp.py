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


# ---------- М1: заявки, профиль, автонастройка ----------
async def add_lead(maker, user_id, text="Нужен сайт для кафе, бюджет 30к", status="new", author=900, sent=True):
    from app.models import Delivery, Lead

    async with maker() as s:
        lead = Lead(author_id=author, username="client_u", name="Клиент", group_title="Группа", text=text,
                    text_hash=f"h{author}{text}"[:40], city="Москва")
        s.add(lead)
        await s.flush()
        d = Delivery(lead_id=lead.id, user_id=user_id, status=status, sent=sent)
        s.add(d)
        await s.commit()
        return d.id


def json_put(client, path, user_id, body):
    return client.put(path, json=body, headers=tma(user_id))


async def test_leads_list_is_private_filtered_and_paged(client, maker):
    await make_user(maker, 20)
    await make_user(maker, 21)
    mine = [await add_lead(maker, 20, text=f"заявка {i}", author=1000 + i) for i in range(23)]
    await add_lead(maker, 20, text="отложенная", status="postponed", author=2000)
    await add_lead(maker, 21, text="чужая", author=3000)
    await add_lead(maker, 20, text="ещё не доставлена", author=4000, sent=False)
    first = await (await client.get("/api/leads?status=new", headers=tma(20))).json()
    assert len(first["items"]) == 20 and first["has_more"] is True
    assert first["items"][0]["id"] == mine[-1] and first["items"][0]["contact"] == "https://t.me/client_u"
    rest = await (await client.get(f"/api/leads?status=new&before={first['items'][-1]['id']}", headers=tma(20))).json()
    assert len(rest["items"]) == 3 and rest["has_more"] is False
    later = await (await client.get("/api/leads?status=postponed", headers=tma(20))).json()
    assert [i["text"] for i in later["items"]] == ["отложенная"]
    assert (await client.get("/api/leads?status=хм", headers=tma(20))).status == 400


async def test_lead_actions_take_later_spam_and_ownership(client, maker):
    from sqlalchemy import select

    from app.models import BlacklistEntry, Client, Delivery

    await make_user(maker, 22)
    await make_user(maker, 23)
    d_take = await add_lead(maker, 22, author=910)
    r = await client.post(f"/api/leads/{d_take}/take", headers=tma(22))
    body = await r.json()
    assert r.status == 200 and body["status"] == "taken" and body["client_id"]
    async with maker() as s:
        assert (await s.scalar(select(Client).where(Client.user_id == 22, Client.tg_id == 910))) is not None
    d_later = await add_lead(maker, 22, author=911)
    assert (await (await client.post(f"/api/leads/{d_later}/later", headers=tma(22))).json())["status"] == "postponed"
    d_spam = await add_lead(maker, 22, author=912)
    assert (await (await client.post(f"/api/leads/{d_spam}/spam", headers=tma(22))).json())["status"] == "spam"
    async with maker() as s:
        assert await s.get(BlacklistEntry, 912) is None  # один голос — ещё не общий чёрный список
    assert (await client.post(f"/api/leads/{d_take}/reject", headers=tma(23))).status == 404  # чужая заявка
    assert (await client.post(f"/api/leads/{d_later}/client", headers=tma(22))).status == 403  # «Это клиент» — только админ
    assert (await client.post(f"/api/leads/{d_later}/взлом", headers=tma(22))).status == 400
    assert (await client.post(f"/api/leads/{d_later}/later")).status == 401
    async with maker() as s:
        assert (await s.get(Delivery, d_later)).status == "postponed"


async def test_reply_texts(client, maker):
    await make_user(maker, 24, prices="сайт от 15 000 ₽")
    d = await add_lead(maker, 24)
    tpl = await (await client.get(f"/api/leads/{d}/reply?kind=template", headers=tma(24))).json()
    drf = await (await client.get(f"/api/leads/{d}/reply?kind=draft", headers=tma(24))).json()
    assert "15 000" in tpl["text"] and tpl["contact"] == "https://t.me/client_u"
    assert drf["text"] and "15 000" in drf["text"]
    assert (await client.get(f"/api/leads/{d}/reply?kind=x", headers=tma(24))).status == 400
    assert (await client.get(f"/api/leads/{d}/reply", headers=tma(25))).status == 403  # нет такого пользователя


async def test_profile_read_and_save_with_validation(client, maker):
    from sqlalchemy import select

    from app.models import UserNegative

    await make_user(maker, 26, niches=("Сайты",))
    prof = await (await client.get("/api/profile", headers=tma(26))).json()
    on = [n for n in prof["niches"] if n["on"]]
    assert len(on) == 1 and on[0]["name"] == "Сайты"
    other = next(n["id"] for n in prof["niches"] if not n["on"])
    r = await json_put(client, "/api/profile", 26, {"niches": [other], "city": "Казань", "only_city": True, "quiet_hours": "23-8",
                                                    "reply_template": "Здравствуйте!", "prices": "от 1000", "paused": True,
                                                    "negatives": ["бесплатно", "Бесплатно", " вакансия ", ""]})
    body = await r.json()
    assert r.status == 200 and body["profile"]["city"] == "Казань" and body["profile"]["only_city"] and body["profile"]["paused"]
    assert [n["id"] for n in body["profile"]["niches"] if n["on"]] == [other]
    assert body["profile"]["negatives"] == ["бесплатно", "вакансия"] and body["profile"]["quiet_hours"] == "23-8"
    # ошибки ввода не меняют ничего
    assert (await json_put(client, "/api/profile", 26, {"niches": []})).status == 422
    assert (await json_put(client, "/api/profile", 26, {"quiet_hours": "ночью"})).status == 422
    assert (await json_put(client, "/api/profile", 26, {"prices": "я" * 3000})).status == 422
    assert (await json_put(client, "/api/profile", 26, {"niches": [999999]})).status == 422
    assert (await client.put("/api/profile", data="не json", headers=tma(26))).status == 400
    # пустой город — «вся Россия», «только мой город» тогда выключается
    r = await json_put(client, "/api/profile", 26, {"city": ""})
    assert (await r.json())["profile"]["only_city"] is False
    async with maker() as s:
        assert len((await s.scalars(select(UserNegative).where(UserNegative.user_id == 26))).all()) == 2


async def test_autosetup_api(client, maker):
    await make_user(maker, 27)
    info = await (await client.get("/api/autosetup", headers=tma(27))).json()
    assert "Бесплатный тариф" in info["intro"] and {p["what"] for p in info["plan"]} == {"Режим тишины", "Минус-слова"}
    assert (await (await client.post("/api/autosetup", headers=tma(27))).json())["applied"] == 2
    prof = await (await client.get("/api/profile", headers=tma(27))).json()
    assert prof["quiet_hours"] == "23-8" and len(prof["negatives"]) == 4
    assert (await (await client.get("/api/autosetup", headers=tma(27))).json())["plan"] == []


# ---------- М2: CRM и друзья ----------
async def test_crm_create_list_and_privacy(client, maker):
    await make_user(maker, 30)
    await make_user(maker, 31)
    r = await client.post("/api/clients", json={"name": "Марат", "username": "@marat_shop", "phone": "+79001234567"}, headers=tma(30))
    card = await r.json()
    assert r.status == 201 and card["username"] == "marat_shop" and card["stage"] == "new" and card["events"][0]["text"] == "Добавлен вручную"
    assert (await client.post("/api/clients", json={"name": "  "}, headers=tma(30))).status == 422
    assert (await client.post("/api/clients", data="не json", headers=tma(30))).status == 400
    for i in range(22):
        await client.post("/api/clients", json={"name": f"Клиент {i}"}, headers=tma(30))
    await client.post("/api/clients", json={"name": "Чужой"}, headers=tma(31))
    page1 = await (await client.get("/api/clients", headers=tma(30))).json()
    assert len(page1["items"]) == 20 and page1["has_more"] and page1["counts"] == {"new": 23}
    page2 = await (await client.get("/api/clients?offset=20", headers=tma(30))).json()
    assert len(page2["items"]) == 3 and not page2["has_more"]
    found = await (await client.get("/api/clients?q=marat", headers=tma(30))).json()
    assert [c["name"] for c in found["items"]] == ["Марат"]
    assert (await client.get("/api/clients?stage=плохой", headers=tma(30))).status == 400
    assert (await client.get(f"/api/clients/{card['id']}", headers=tma(31))).status == 404  # чужой клиент
    assert (await client.patch(f"/api/clients/{card['id']}", json={"stage": "done"}, headers=tma(31))).status == 404


async def test_crm_patch_stage_fields_note_and_reminder(client, maker):
    await make_user(maker, 32)
    cid = (await (await client.post("/api/clients", json={"name": "Олег"}, headers=tma(32))).json())["id"]
    url = f"/api/clients/{cid}"
    card = await (await client.patch(url, json={"stage": "thinking"}, headers=tma(32))).json()
    assert card["stage"] == "thinking" and card["reminder"]  # «думает» — напоминание через 2 дня само
    card = await (await client.patch(url, json={"budget": "30 000 ₽", "phone": "+7900", "note": "Позвонить вечером"}, headers=tma(32))).json()
    assert card["budget"] == 30000 and card["phone"] == "+7900" and "Позвонить вечером" in card["notes"]
    card = await (await client.patch(url, json={"note": "Второй звонок"}, headers=tma(32))).json()
    assert card["notes"] == "Позвонить вечером\nВторой звонок"
    assert card["events"][0]["text"].startswith("Заметка")
    assert (await client.patch(url, json={"stage": "взлом"}, headers=tma(32))).status == 422
    assert (await client.patch(url, json={"budget": "много"}, headers=tma(32))).status == 422
    assert (await client.patch(url, json={"remind_days": 0}, headers=tma(32))).status == 422
    assert (await client.patch(url, json={"remind_days": 91}, headers=tma(32))).status == 422
    assert (await client.patch(url, json={"note": "я" * 1001}, headers=tma(32))).status == 422
    card = await (await client.patch(url, json={"remind_days": 3}, headers=tma(32))).json()
    assert card["reminder"]


async def test_referrals_api(client, maker, monkeypatch):
    from types import SimpleNamespace

    from app.bot import notify

    async def me():
        return SimpleNamespace(username="klientradarbot")

    monkeypatch.setattr(notify, "_bot", SimpleNamespace(me=me))
    await make_user(maker, 33)
    await make_user(maker, 34, referred_by=33, ref_rewarded=True)
    await make_user(maker, 35, referred_by=33)
    data = await (await client.get("/api/referrals", headers=tma(33))).json()
    assert data["link"].startswith("https://t.me/klientradarbot?start=") and data["invited"] == 2 and data["done"] == 1
    assert data["share"].startswith("https://t.me/share/url?url=") and data["days"] == 7
