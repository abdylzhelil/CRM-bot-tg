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


# ---------- каталог, тариф, статистика ----------
async def add_group(maker, tg_id, **kw):
    from app.models import Group, now

    async with maker() as s:
        g = Group(tg_id=tg_id, title=kw.pop("title", f"Группа {tg_id}"), username=kw.pop("username", f"g{tg_id}"),
                  members=kw.pop("members", 500), status=kw.pop("status", "joined"), catalog=kw.pop("catalog", True),
                  submitted_at=now(), **kw)
        s.add(g)
        await s.commit()
        return g.id


async def test_catalog_browse_my_groups_and_rating(client, maker):
    await make_user(maker, 40, city="")
    await make_user(maker, 41)
    await add_group(maker, 5001, title="Сайты Москва", leads_count=12, submitted_by=41)
    await add_group(maker, 5002, title="Скрытая", catalog=False, submitted_by=41)
    mine = await add_group(maker, 5003, title="Моя группа", leads_count=25, bonus_rewarded=25, submitted_by=40)
    await add_group(maker, 5004, title="Черновик", status="draft", catalog=False, submitted_by=40)
    data = await (await client.get("/api/catalog?scope=all", headers=tma(40))).json()
    titles = [g["title"] for g in data["groups"]]
    assert "Сайты Москва" in titles and "Моя группа" in titles and "Скрытая" not in titles
    assert data["groups"][0]["link"].startswith("https://t.me/") and data["bonus_leads"] == 5
    assert (await client.get("/api/catalog?scope=?", headers=tma(40))).status == 400
    my = await (await client.get("/api/catalog/my", headers=tma(40))).json()
    assert [g["title"] for g in my["items"]] == ["Моя группа"] and my["items"][0]["bonus_days"] == 5 and my["bonus_days"] == 5
    assert my["items"][0]["id"] == mine
    top = await (await client.get("/api/rating", headers=tma(40))).json()
    assert top["top"][0]["leads"] == 25 and top["top"][0]["me"] and top["place"] == 1


async def test_ad_order_checks_leads_and_notifies_admin(client, maker, tgbot):
    from app.services import settings

    await make_user(maker, 42)
    await make_user(maker, 43)
    poor = await add_group(maker, 5010, leads_count=3, submitted_by=42)
    rich = await add_group(maker, 5011, leads_count=30, submitted_by=42)
    r = await client.post(f"/api/catalog/ads/{poor}", headers=tma(42))
    assert r.status == 422 and "Нужно" in (await r.json())["message"]
    assert (await client.post(f"/api/catalog/ads/{rich}", headers=tma(43))).status == 422  # чужая группа
    assert (await client.post(f"/api/catalog/ads/{rich}", headers=tma(42))).status == 200
    assert any("Заказ рекламы" in t for t in tgbot.texts(ADMIN_ID))
    assert (await client.post(f"/api/catalog/ads/{rich}", headers=tma(42))).status == 422  # уже отправлена
    my = await (await client.get("/api/catalog/my", headers=tma(42))).json()
    assert next(i for i in my["items"] if i["id"] == rich)["ad_pending"] is True


async def test_plans_and_payment_link(client, maker, monkeypatch):
    from sqlalchemy import select

    from app.bot import notify
    from app.models import PendingLink
    from app.services import payments, settings
    from types import SimpleNamespace

    async def me():
        return SimpleNamespace(username="klientradarbot")

    monkeypatch.setattr(notify, "_bot", SimpleNamespace(me=me))
    await make_user(maker, 44)
    plans = await (await client.get("/api/plans", headers=tma(44))).json()
    assert plans["packages"] == [] and plans["can_pay"] is False and "Бесплатный тариф" in plans["intro"]
    async with maker() as s:
        await settings.put(s, "price_30", "490")
        await settings.put(s, "price_90", "1290")
    assert (await client.post("/api/pay", json={"days": 30}, headers=tma(44))).status == 503  # ключей ЮKassa нет
    monkeypatch.setattr(config, "YOOKASSA_SHOP_ID", "1")
    monkeypatch.setattr(config, "YOOKASSA_SECRET_KEY", "k")
    sent = {}

    async def fake_api(method, path, body=None, key=""):
        sent.update(body=body)
        return {"id": "yk-1", "confirmation": {"confirmation_url": "https://yoomoney.ru/pay/1"}}

    monkeypatch.setattr(payments, "_api", fake_api)
    plans = await (await client.get("/api/plans", headers=tma(44))).json()
    assert plans["packages"] == [{"days": 30, "price": 490}, {"days": 90, "price": 1290}] and plans["can_pay"] is True
    assert (await client.post("/api/pay", json={"days": 365}, headers=tma(44))).status == 422  # пакет не продаётся
    assert (await client.post("/api/pay", json={"days": "хак"}, headers=tma(44))).status == 422
    r = await client.post("/api/pay", json={"days": 30}, headers=tma(44))
    assert r.status == 200 and (await r.json())["url"] == "https://yoomoney.ru/pay/1"
    assert sent["body"]["amount"]["value"] == "490.00" and sent["body"]["metadata"]["user_id"] == "44"  # цена и человек — с сервера
    async with maker() as s:
        assert (await s.scalar(select(PendingLink).where(PendingLink.yk_id == "yk-1"))).days == 30
    assert (await client.post("/api/pay", json={"days": 30}, headers=tma(ADMIN_ID))).status == 403


async def test_stats_api(client, maker):
    await make_user(maker, 45)
    await add_lead(maker, 45, status="taken", author=950)
    await add_lead(maker, 45, author=951)
    st = await (await client.get("/api/stats", headers=tma(45))).json()
    assert st["total"] == 2 and st["taken"] == 1 and st["conversion"] == 50 and st["today"] == 2


# ---------- М4: админка ----------
async def ensure_admin(maker):
    from app.models import User

    async with maker() as s:
        if await s.get(User, ADMIN_ID) is None:
            s.add(User(tg_id=ADMIN_ID, name="Админ", is_admin=True, onboarded=True, plan="full"))
            await s.commit()


async def test_admin_api_is_closed_to_regular_users(client, maker):
    await make_user(maker, 50)
    await make_user(maker, 51, plan="full")
    for uid in (50, 51):
        for method, path in [("get", "/api/admin/status"), ("get", "/api/admin/settings"), ("get", "/api/admin/users"),
                             ("get", "/api/admin/users/50"), ("put", "/api/admin/settings/mode"), ("put", "/api/admin/scout"),
                             ("post", "/api/admin/presets/launch"), ("post", "/api/admin/users/50/plan")]:
            r = await getattr(client, method)(path, headers=tma(uid))
            assert r.status == 403, (uid, method, path)
    assert (await client.get("/api/admin/status")).status == 401


async def test_admin_status_settings_presets_and_scout(client, maker):
    from app.services import settings as st

    await ensure_admin(maker)
    stat = await (await client.get("/api/admin/status", headers=tma(ADMIN_ID))).json()
    assert {"users", "paid", "scout_paused", "selling"} <= set(stat["status"]) and stat["ok"] is not None
    assert {p["id"] for p in stat["presets"]} == {"launch", "careful"} and stat["presets"][0]["changes"]
    cfg = await (await client.get("/api/admin/settings", headers=tma(ADMIN_ID))).json()
    assert len(cfg["sections"]) == len(st.SECTIONS) and all(i["hint"] for sec in cfg["sections"] for i in sec["items"])
    h = tma(ADMIN_ID)
    assert (await (await client.put("/api/admin/settings/free_daily_limit", json={"value": "8"}, headers=h)).json())["value"] == "8"
    async with maker() as s:
        assert await st.get(s, "free_daily_limit") == "8"
    for key, bad in [("mode", "x"), ("report_hour", "30"), ("price_30", "много"), ("ai_model", "gpt"), ("channel_hour", "24"), ("нет_такой", "1")]:
        assert (await client.put(f"/api/admin/settings/{key}", json={"value": bad}, headers=h)).status == 422, key
    assert (await client.put("/api/admin/settings/mode", json={}, headers=h)).status == 400
    r = await client.put("/api/admin/settings/free_daily_limit", json={"value": st.DEFAULTS["free_daily_limit"]}, headers=h)
    assert (await r.json())["value"] == "5"  # сброс по умолчанию
    assert (await client.post("/api/admin/presets/взлом", headers=h)).status == 404
    assert (await (await client.post("/api/admin/presets/careful", headers=h)).json())["changed"] > 0
    async with maker() as s:
        assert await st.get(s, "default_join_limit") == "5"
    assert (await client.put("/api/admin/scout", json={"paused": "да"}, headers=h)).status == 400
    await client.put("/api/admin/scout", json={"paused": True}, headers=h)
    async with maker() as s:
        assert await st.get(s, "scout_paused") == "1"


async def test_admin_users_search_card_and_plan(client, maker, tgbot):
    await ensure_admin(maker)
    await make_user(maker, 52, city="Казань")
    await make_user(maker, 53, city="Москва")
    h = tma(ADMIN_ID)
    lst = await (await client.get("/api/admin/users", headers=h)).json()
    assert lst["total"] >= 3 and {i["id"] for i in lst["items"]} >= {52, 53}
    assert [i["id"] for i in (await (await client.get("/api/admin/users?q=52", headers=h)).json())["items"]] == [52]
    assert [i["id"] for i in (await (await client.get("/api/admin/users?q=user53", headers=h)).json())["items"]] == [53]
    card = await (await client.get("/api/admin/users/52", headers=h)).json()
    assert card["city"] == "Казань" and card["niches"] and card["paid"] is False
    assert (await client.get("/api/admin/users/999999", headers=h)).status == 404
    r = await client.post("/api/admin/users/52/plan", json={"value": "30"}, headers=h)
    paid = await r.json()
    assert r.status == 200 and paid["paid"] and paid["until"]
    assert any("Полный тариф" in t for t in tgbot.texts(52))  # человеку ушло уведомление
    assert (await (await client.post("/api/admin/users/52/plan", json={"value": "0"}, headers=h)).json())["until"] is None  # бессрочно
    assert (await (await client.post("/api/admin/users/52/plan", json={"value": "free"}, headers=h)).json())["paid"] is False
    for bad in ("много", "-5", "99999"):
        assert (await client.post("/api/admin/users/52/plan", json={"value": bad}, headers=h)).status == 422
    assert (await client.post("/api/admin/users/999999/plan", json={"value": "30"}, headers=h)).status == 404


# ---------- М5: ошибки и кэш ----------
async def test_server_error_becomes_friendly_json(client, maker, tgbot, monkeypatch):
    from app.web import api

    await make_user(maker, 60)

    async def boom(request):
        raise RuntimeError("секретная внутренняя причина")

    monkeypatch.setattr(api, "profile_get", boom)
    app = web_server.make_app()
    c2 = TestClient(TestServer(app))
    await c2.start_server()
    try:
        r = await c2.get("/api/profile", headers=tma(60))
        body = await r.json()
        assert r.status == 500 and body["error"] == "server" and "секретная" not in body["message"]
        assert any("Ошибка в кабинете" in t for t in tgbot.texts(ADMIN_ID))
    finally:
        await c2.close()


async def test_static_is_revalidated_not_stuck(client):
    page = await client.get("/app/")
    assert page.headers["Cache-Control"] == "no-cache"
    js = await client.get("/app/app.js")
    assert js.headers["Cache-Control"] == "no-cache" and js.headers.get("ETag")
    again = await client.get("/app/app.js", headers={"If-None-Match": js.headers["ETag"]})
    assert again.status == 304  # ничего не качаем заново, пока файл не изменился


# ---------- проверка на ошибки: границы настроек, запуск, заголовки ----------
def test_setting_validation_blocks_dangerous_values():
    from app.services import settings as st

    for key, bad in [("max_recipients", "0"), ("rotation_days", "0"), ("search_interval_h", "0"), ("channel_poll_min", "0"),
                     ("spam_block", "101"), ("ref_percent", "150"), ("autorenew", "2"), ("ai_enabled", "да"),
                     ("scout_paused", "5"), ("bonus_leads", "0"), ("price_30", "99999999")]:
        try:
            st.validate(key, bad)
        except st.SettingError:
            continue
        raise AssertionError(f"{key}={bad} должно отклоняться")
    for key, good in [("max_recipients", "5"), ("rotation_days", "30"), ("spam_block", "60"), ("autorenew", "1"),
                      ("ai_enabled", "0"), ("price_30", "490"), ("free_daily_limit", "0"), ("mode", "auto")]:
        st.validate(key, good)
    # все значения по умолчанию сами проходят проверку (иначе «↩️ По умолчанию» сломалось бы)
    for key in st.LABELS:
        st.validate(key, st.DEFAULTS[key])


async def test_web_server_really_starts(monkeypatch):
    import socket

    import aiohttp

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    monkeypatch.setattr(config, "WEBAPP_URL", "https://example.test")
    monkeypatch.setattr(config, "WEBAPP_PORT", port)
    runner = await web_server.start()
    try:
        async with aiohttp.ClientSession() as http:
            async with http.get(f"http://127.0.0.1:{port}/healthz") as r:
                assert r.status == 200 and (await r.json()) == {"ok": True}
    finally:
        await runner.cleanup()
    monkeypatch.setattr(config, "WEBAPP_URL", "")
    assert await web_server.start() is None  # без адреса кабинет выключен


async def test_error_response_keeps_security_headers(client, maker, tgbot, monkeypatch):
    from app.web import api

    await make_user(maker, 61)

    async def boom(request):
        raise RuntimeError("x")

    monkeypatch.setattr(api, "profile_get", boom)
    c2 = TestClient(TestServer(web_server.make_app()))
    await c2.start_server()
    try:
        r = await c2.get("/api/profile", headers=tma(61))
        assert r.status == 500 and "default-src 'self'" in r.headers["Content-Security-Policy"]
    finally:
        await c2.close()


async def test_saving_niches_keeps_own_pending_niche(client, maker):
    from sqlalchemy import select

    from app.models import Category, UserCategory

    await make_user(maker, 62, niches=("Сайты",))
    async with maker() as s:
        mine = Category(name="Ремонт квартир", emoji="📌", proposed_by=62, pending=True)
        s.add(mine)
        await s.flush()
        s.add(UserCategory(user_id=62, category_id=mine.id))
        await s.commit()
        pending_id = mine.id
    prof = await (await client.get("/api/profile", headers=tma(62))).json()
    assert pending_id not in [n["id"] for n in prof["niches"]]  # на проверке — не показываем
    other = next(n["id"] for n in prof["niches"] if not n["on"])
    r = await client.put("/api/profile", json={"niches": [other]}, headers=tma(62))
    assert r.status == 200
    async with maker() as s:
        ids = set((await s.scalars(select(UserCategory.category_id).where(UserCategory.user_id == 62))).all())
    assert pending_id in ids and other in ids  # своя ниша не потерялась, выбранная добавилась


async def test_healthz_checks_database_and_allows_head(client, monkeypatch):
    r = await client.get("/healthz")
    assert r.status == 200 and (await r.json()) == {"ok": True}
    assert (await client.head("/healthz")).status == 200  # многие сервисы проверки шлют HEAD

    class Broken:
        async def __aenter__(self):
            raise RuntimeError("база недоступна")

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(web_server, "Session", lambda: Broken())
    r = await client.get("/healthz")
    assert r.status == 503 and (await r.json())["ok"] is False


async def test_cabinet_shows_card_and_unbinds_it(client, maker, monkeypatch):
    from app import config as cfg
    from app.models import User

    await make_user(maker, 63, plan="full", renew_method="pm-1", renew_card="Mir •••• 4242", renew_days=30)
    plans = await (await client.get("/api/plans", headers=tma(63))).json()
    assert plans["renew"] == {"card": "Mir •••• 4242", "days": 30}
    assert (await client.post("/api/renew/off")).status == 401
    r = await client.post("/api/renew/off", headers=tma(63))
    assert r.status == 200 and "отвязана" in (await r.json())["message"]
    async with maker() as s:
        u = await s.get(User, 63)
        assert (u.renew_method, u.renew_card) == ("", "") and u.plan == "full"
    assert (await (await client.get("/api/plans", headers=tma(63))).json())["renew"] is None
    assert (await client.post("/api/renew/off", headers=tma(63))).status == 404  # повторно — нечего отвязывать


async def test_crm_rename_cancel_reminder_and_delete(client, maker):
    await make_user(maker, 33)
    await make_user(maker, 34)
    cid = (await (await client.post("/api/clients", json={"phone": "+79005556677"}, headers=tma(33))).json())["id"]
    url = f"/api/clients/{cid}"
    card = await (await client.patch(url, json={"name": "Ирина", "remind_days": 3}, headers=tma(33))).json()
    assert card["name"] == "Ирина" and card["reminder"]
    card = await (await client.patch(url, json={"remind_off": True}, headers=tma(33))).json()
    assert card["reminder"] is None  # напоминание можно отменить
    assert (await client.delete(url, headers=tma(34))).status == 404  # чужого не удалить
    assert (await client.delete(url, headers=tma(33))).status == 200
    assert (await client.get(url, headers=tma(33))).status == 404


async def test_lead_restore_from_spam_and_spam_tab(client, maker):
    from sqlalchemy import select

    from app.models import BlacklistEntry, Delivery, Lead, SpamVote

    await make_user(maker, 35)
    async with maker() as s:
        lead = Lead(group_id=0, group_title="Г", author_id=777, text="Нужен сайт", text_hash="h")
        s.add(lead)
        await s.commit()
        d = Delivery(lead_id=lead.id, user_id=35, deliver_after=lead.created_at, sent=True)
        s.add(d)
        await s.commit()
    assert (await client.post(f"/api/leads/{d.id}/spam", headers=tma(35))).status == 200
    spam = await (await client.get("/api/leads?status=spam", headers=tma(35))).json()
    assert [x["id"] for x in spam["items"]] == [d.id]  # вкладка «Спам» в кабинете
    r = await (await client.post(f"/api/leads/{d.id}/restore", headers=tma(35))).json()
    assert r["status"] == "new"
    async with maker() as s:
        assert await s.scalar(select(SpamVote).where(SpamVote.author_id == 777)) is None  # голос снят
        assert await s.get(BlacklistEntry, 777) is None
    assert (await client.post(f"/api/leads/{d.id}/restore", headers=tma(35))).status == 403  # новую «возвращать» нечего
