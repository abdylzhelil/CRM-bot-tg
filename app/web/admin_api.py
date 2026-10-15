"""Админка кабинета (/api/admin/*). Доступ — только администраторам (проверка в middleware).

Рассылка, одобрение групп, лицензии и чёрный список остаются в боте: там у них подтверждения и карточки.
"""
from aiohttp import web
from sqlalchemy import func, select

from app.db import Session
from app.models import Category, Client, Delivery, User, UserCategory
from app.services import admin_status, autosetup, settings
from app.services import users_admin as users_svc
from app.services.distribute import delivered_today, is_paid
from app.web.api import _int, err, read_json


async def status_get(request: web.Request) -> web.Response:
    async with Session() as s:
        data = await admin_status.collect(s)
        ok, bad = await autosetup.admin_checklist(s)
        presets = []
        for name, (icon, title, desc, _) in settings.PRESETS.items():
            diff = await settings.preset_diff(s, name)
            presets.append({"id": name, "icon": icon, "title": title, "desc": desc, "changes": [
                {"key": k, "label": settings.SHORT.get(k, settings.LABELS[k]), "old": old, "new": new} for k, old, new in diff]})
    return web.json_response({"status": data, "ok": ok, "bad": bad, "presets": presets})


async def scout_put(request: web.Request) -> web.Response:
    data = await read_json(request)
    if data is None or not isinstance(data.get("paused"), bool):
        return err("bad_request", "Нужно paused: true или false")
    async with Session() as s:
        await settings.put(s, "scout_paused", "1" if data["paused"] else "0")
    return web.json_response({"ok": True, "paused": data["paused"]})


async def settings_get(request: web.Request) -> web.Response:
    async with Session() as s:
        sections = []
        for icon, title, keys in settings.SECTIONS:
            items = [{"key": k, "label": settings.LABELS[k], "short": settings.SHORT.get(k, settings.LABELS[k]),
                      "hint": settings.HINTS.get(k, ""), "value": await settings.get(s, k), "default": settings.DEFAULTS[k]}
                     for k in keys]
            sections.append({"icon": icon, "title": title, "items": items})
    return web.json_response({"sections": sections})


async def setting_put(request: web.Request) -> web.Response:
    key = request.match_info["key"]
    data = await read_json(request)
    if data is None or "value" not in data:
        return err("bad_request", "Нужно поле value")
    try:
        value = settings.validate(key, str(data["value"]))
    except settings.SettingError as e:
        return err("invalid", str(e), 422)
    async with Session() as s:
        await settings.put(s, key, value)
    return web.json_response({"ok": True, "key": key, "value": value})


async def preset_apply(request: web.Request) -> web.Response:
    name = request.match_info["name"]
    if name not in settings.PRESETS:
        return err("not_found", "Нет такого пресета", 404)
    async with Session() as s:
        n = await settings.apply_preset(s, name)
    return web.json_response({"ok": True, "changed": n})


def user_item(u: User) -> dict:
    return {"id": u.tg_id, "name": u.name, "username": u.username, "city": u.city, "paid": is_paid(u),
            "blocked": u.blocked, "onboarded": u.onboarded, "admin": u.is_admin}


async def users_list(request: web.Request) -> web.Response:
    q = request.query.get("q", "").strip()[:64]
    offset = max(_int(request.query.get("offset")), 0)
    async with Session() as s:
        query = select(User).order_by(User.last_seen.desc())
        if q:
            raw = q.lstrip("@")
            if raw.isascii() and raw.isdigit() and len(raw) < 19:
                query = query.where(User.tg_id == int(raw))
            elif q.startswith("@"):
                query = query.where(User.username.ilike(raw))
            else:
                query = query.where((User.name.ilike(f"%{q}%")) | (User.username.ilike(f"%{raw}%")))
        rows = (await s.scalars(query.offset(offset).limit(21))).all()
        total = await s.scalar(select(func.count()).select_from(User)) or 0
    return web.json_response({"items": [user_item(u) for u in rows[:20]], "has_more": len(rows) > 20, "total": total})


async def user_card(s, u: User) -> dict:
    niches = (await s.scalars(select(Category.name).join(UserCategory, UserCategory.category_id == Category.id)
                              .where(UserCategory.user_id == u.tg_id))).all()
    item = user_item(u)
    item.update({
        "until": u.plan_until.isoformat() if u.plan_until else None, "niches": list(niches),
        "today": await delivered_today(s, u.tg_id),
        "total": await s.scalar(select(func.count()).select_from(Delivery).where(Delivery.user_id == u.tg_id)) or 0,
        "clients": await s.scalar(select(func.count()).select_from(Client).where(Client.user_id == u.tg_id)) or 0,
        "since": u.created_at.isoformat(), "last_seen": u.last_seen.isoformat(),
    })
    return item


async def user_get(request: web.Request) -> web.Response:
    async with Session() as s:
        u = await s.get(User, _int(request.match_info["id"]))
        if u is None:
            return err("not_found", "Пользователь не найден", 404)
        return web.json_response(await user_card(s, u))


async def user_plan(request: web.Request) -> web.Response:
    """value: free | 0 (бессрочно) | число дней. Человеку уходит то же уведомление, что и из бота."""
    from app.bot import notify

    data = await read_json(request)
    if data is None or "value" not in data:
        return err("bad_request", "Нужно поле value")
    async with Session() as s:
        u = await s.get(User, _int(request.match_info["id"]))
        if u is None:
            return err("not_found", "Пользователь не найден", 404)
        try:
            notice = users_svc.set_plan(u, str(data["value"]).strip())
        except users_svc.PlanError as e:
            return err("invalid", str(e), 422)
        await s.commit()
        card = await user_card(s, u)
    try:
        await notify._bot.send_message(u.tg_id, notice)
    except Exception:
        pass  # человек мог заблокировать бота — тариф всё равно изменён
    return web.json_response(card)
