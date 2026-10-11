"""HTTP-приём от бота модерации: он работает «разведчиком» в своих группах.

POST /feed/chats    — список групп бота модерации; в ответ — какие из них включены
POST /feed/message  — сообщение из группы (только из включённых)
Авторизация: заголовок «Authorization: Bearer <FEED_TOKEN>». Сервер слушает внутреннюю сеть Docker.
"""
import hmac
import logging

from aiohttp import web
from sqlalchemy import select

from app import config
from app.bot import notify
from app.bot.common import esc
from app.db import Session
from app.feed.links import message_link
from app.models import FeedChat, Group, now
from app.services import intake
from app.services.antispam import AuthorInfo
from app.services.pipeline import Incoming

log = logging.getLogger(__name__)


@web.middleware
async def auth(request: web.Request, handler):
    token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    if not config.FEED_TOKEN or not hmac.compare_digest(token, config.FEED_TOKEN):
        return web.json_response({"error": "unauthorized"}, status=401)
    return await handler(request)


async def sync_chats(request: web.Request) -> web.Response:
    data = await request.json()
    new: list[FeedChat] = []
    async with Session() as s:
        known = {c.tg_id: c for c in (await s.scalars(select(FeedChat))).all()}
        first_sync = not known
        for item in data.get("chats", []):
            tg_id = int(item["id"])
            row = known.get(tg_id)
            if row is None:
                row = FeedChat(tg_id=tg_id)
                s.add(row)
                new.append(row)
            row.title = str(item.get("title") or "")[:255]
            row.username = str(item.get("username") or "")[:64]
            row.own = bool(item.get("own"))
            row.seen_at = now()
        ids = [int(item["id"]) for item in data.get("chats", [])]
        if ids:  # общая база: разведчик не пойдёт туда, где уже есть бот модерации
            await s.execute(Group.__table__.update().where(
                Group.tg_id.in_(ids), Group.status.in_(["candidate", "approved"])).values(status="covered"))
        await s.commit()
        enabled = [c.tg_id for c in (await s.scalars(select(FeedChat).where(FeedChat.enabled))).all()]
    if new and not first_sync:
        names = ", ".join(f"«{esc(c.title)}»" for c in new[:5])
        await notify._send(
            f"🛡 Бот модерации добавлен в новые группы: {names}"
            f"{' и др.' if len(new) > 5 else ''}.\nВключить передачу заявок: 🔍 Группы → 🛡 Бот модерации."
        )
    elif new:
        await notify._send(
            f"🛡 Бот модерации подключён к CRM. Групп: {len(new)}. "
            "Выберите, из каких брать заявки: 🔍 Группы → 🛡 Бот модерации."
        )
    return web.json_response({"enabled": enabled})


async def ensure_group(s, chat: dict) -> None:
    tg_id = int(chat["id"])
    g = await s.scalar(select(Group).where(Group.tg_id == tg_id))
    if g is None:
        s.add(Group(tg_id=tg_id, title=chat.get("title") or "", username=chat.get("username") or "",
                    status="joined", joined_at=now()))
    else:
        g.title = chat.get("title") or g.title
        if g.status != "joined":
            g.status = "joined"
    await s.commit()


async def message(request: web.Request) -> web.Response:
    data = await request.json()
    chat, user, text = data.get("chat") or {}, data.get("user") or {}, (data.get("text") or "").strip()
    if not chat.get("id") or not user.get("id") or len(text) < 8:
        return web.json_response({"ok": True, "skipped": "empty"})
    async with Session() as s:
        row = await s.get(FeedChat, int(chat["id"]))
        if row is None or not row.enabled:
            return web.json_response({"ok": True, "skipped": "disabled"})
        await ensure_group(s, chat)
    msg_id = int(data.get("message_id") or 0)
    lead = await intake.handle(Incoming(
        text=text,
        author=AuthorInfo(
            user_id=int(user["id"]),
            username=user.get("username") or "",
            first_name=user.get("first_name") or "",
            last_name=user.get("last_name") or "",
            bot=bool(user.get("is_bot")),
        ),
        group_tg_id=int(chat["id"]),
        group_title=chat.get("title") or "",
        message_id=msg_id,
        link=message_link(int(chat["id"]), chat.get("username"), msg_id),
    ))
    return web.json_response({"ok": True, "lead": lead.id if lead else None})


async def health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


def make_app() -> web.Application:
    app = web.Application(middlewares=[auth], client_max_size=256 * 1024)
    app.router.add_post("/feed/chats", sync_chats)
    app.router.add_post("/feed/message", message)
    app.router.add_get("/feed/health", health)
    return app


async def start() -> web.AppRunner | None:
    if not config.FEED_TOKEN:
        log.info("приём от бота модерации выключен (FEED_TOKEN пуст)")
        return None
    runner = web.AppRunner(make_app())
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", config.FEED_PORT).start()
    log.info("приём от бота модерации: порт %s", config.FEED_PORT)
    return runner
