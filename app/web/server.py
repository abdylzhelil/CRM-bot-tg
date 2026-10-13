"""Веб-сервер мини-приложения: статичные файлы /app/ и JSON-API /api/*.

Права берутся из базы по проверенной подписи Telegram, а не из того, что прислал браузер.
Слушает порт WEBAPP_PORT; наружу его выставляет Caddy (HTTPS). Приём от бота модерации (порт 8090) — отдельный и в интернет не открыт.
"""
import logging
import time
from collections import defaultdict, deque
from datetime import timedelta
from pathlib import Path

from aiohttp import web
from sqlalchemy import func, select

from app import config
from app.db import Session
from app.models import Client, Delivery, User, now
from app.services import settings
from app.services.distribute import is_paid
from app.web.auth import verify_init_data

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parents[2] / "webapp"
RATE_LIMIT = 90  # запросов в минуту на пользователя
_hits: dict[int, deque] = defaultdict(deque)
USER_KEY = web.RequestKey("user", User)

# Что показывать в меню приложения: раздел → кому доступен
SECTIONS = [
    ("home", "Главная", "all"),
    ("leads", "Заявки", "all"),
    ("clients", "Клиенты", "all"),
    ("profile", "Профиль", "all"),
    ("admin", "Управление", "admin"),
]


def allowed_sections(user: User) -> list[dict]:
    return [{"id": i, "title": t} for i, t, who in SECTIONS if who == "all" or (who == "admin" and user.is_admin)]


@web.middleware
async def security_headers(request: web.Request, handler):
    resp = await handler(request)
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self' https://telegram.org; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; base-uri 'none'")
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


def _limited(user_id: int) -> bool:
    q, t = _hits[user_id], time.monotonic()
    while q and t - q[0] > 60:
        q.popleft()
    if len(q) >= RATE_LIMIT:
        return True
    q.append(t)
    return False


@web.middleware
async def auth(request: web.Request, handler):
    if not request.path.startswith("/api/"):
        return await handler(request)
    header = request.headers.get("Authorization", "")
    tg_user = verify_init_data(header.removeprefix("tma ").strip() if header.startswith("tma ") else "", config.BOT_TOKEN)
    if tg_user is None:
        return web.json_response({"error": "unauthorized", "message": "Откройте кабинет из бота"}, status=401)
    async with Session() as s:
        user = await s.get(User, int(tg_user["id"]))
    if user is None or not user.onboarded:
        return web.json_response({"error": "not_registered", "message": "Сначала настройте бота: нажмите /start в чате"}, status=403)
    if user.blocked and not user.is_admin:
        return web.json_response({"error": "blocked", "message": "Доступ закрыт"}, status=403)
    if _limited(user.tg_id):
        return web.json_response({"error": "rate_limited", "message": "Слишком часто, подождите минуту"}, status=429)
    request[USER_KEY] = user
    return await handler(request)


async def me(request: web.Request) -> web.Response:
    user: User = request[USER_KEY]
    day = now() - timedelta(days=1)
    async with Session() as s:
        mine = Delivery.user_id == user.tg_id
        cnt = dict((await s.execute(select(Delivery.status, func.count()).where(mine, Delivery.sent).group_by(Delivery.status))).all())
        today = await s.scalar(select(func.count()).select_from(Delivery).where(mine, Delivery.sent, Delivery.created_at > day)) or 0
        clients = await s.scalar(select(func.count()).select_from(Client).where(Client.user_id == user.tg_id)) or 0
        free_limit = await settings.get_int(s, "free_daily_limit")
        free_delay = await settings.get_int(s, "free_delay_min")
    paid = is_paid(user)
    return web.json_response({
        "id": user.tg_id,
        "name": user.name,
        "role": "admin" if user.is_admin else ("paid" if paid else "free"),
        "plan": {
            "paid": paid,
            "until": user.plan_until.isoformat() if user.plan_until else None,
            "free_limit": None if paid or user.is_admin else free_limit,
            "free_delay_min": None if paid or user.is_admin else free_delay,
        },
        "city": user.city,
        "paused": user.paused,
        "stats": {
            "today": today, "new": cnt.get("new", 0), "postponed": cnt.get("postponed", 0),
            "taken": cnt.get("taken", 0), "clients": clients,
        },
        "sections": allowed_sections(user),
    })


async def health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def index(request: web.Request) -> web.FileResponse:
    return web.FileResponse(STATIC_DIR / "index.html")


def make_app() -> web.Application:
    app = web.Application(middlewares=[security_headers, auth])
    app.router.add_get("/healthz", health)
    app.router.add_get("/api/me", me)
    app.router.add_get("/", index)
    app.router.add_get("/app/", index)
    app.router.add_static("/app/", STATIC_DIR, show_index=False)
    return app


async def start() -> web.AppRunner | None:
    if not config.WEBAPP_URL:
        log.info("мини-приложение выключено (WEBAPP_URL пуст)")
        return None
    runner = web.AppRunner(make_app())
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", config.WEBAPP_PORT).start()
    log.info("мини-приложение: порт %s, адрес %s", config.WEBAPP_PORT, config.WEBAPP_URL)
    return runner
