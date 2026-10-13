"""Методы кабинета: заявки, профиль, автонастройка. Бизнес-логика — в app/services (та же, что у бота)."""
from aiohttp import web
from sqlalchemy import select

from app.db import Session
from app.models import Delivery, Lead, User
from app.services import autosetup, lead_actions, profile_edit, quiet
from app.bot.handlers.leads import category_name
from app.web.keys import USER_KEY

STATUSES = ("new", "postponed", "taken", "rejected")
PAGE = 20


def err(code: str, message: str, status: int = 400) -> web.Response:
    return web.json_response({"error": code, "message": message}, status=status)


async def read_json(request: web.Request) -> dict | None:
    try:
        data = await request.json()
    except Exception:
        return None
    return data if isinstance(data, dict) else None


async def lead_item(s, d: Delivery, lead: Lead) -> dict:
    return {
        "id": d.id,
        "status": d.status,
        "text": lead.text.strip()[:1500],
        "name": lead.name,
        "username": lead.username,
        "group": lead.group_title,
        "category": await category_name(s, lead.category_id),
        "place": lead.city or lead.region,
        "link": lead.link,
        "hot": quiet.is_hot(lead.text),
        "suspicious": lead.suspicious,
        "contact": lead_actions.contact_url(lead),
        "from_colleague": bool(d.from_user),
        "created_at": lead.created_at.isoformat(),
    }


async def leads_list(request: web.Request) -> web.Response:
    user: User = request[USER_KEY]
    status = request.query.get("status", "new")
    if status not in STATUSES:
        return err("bad_status", "Неизвестный статус")
    try:
        before = int(request.query.get("before", "0"))
    except ValueError:
        return err("bad_request", "Некорректный запрос")
    async with Session() as s:
        q = (select(Delivery, Lead).join(Lead, Lead.id == Delivery.lead_id)
             .where(Delivery.user_id == user.tg_id, Delivery.sent, Delivery.status == status))
        if before:
            q = q.where(Delivery.id < before)
        rows = (await s.execute(q.order_by(Delivery.id.desc()).limit(PAGE + 1))).all()
        items = [await lead_item(s, d, lead) for d, lead in rows[:PAGE]]
    return web.json_response({"items": items, "has_more": len(rows) > PAGE})


async def lead_act(request: web.Request) -> web.Response:
    user: User = request[USER_KEY]
    action = request.match_info["action"]
    if action not in lead_actions.ACTIONS:
        return err("bad_action", "Неизвестное действие")
    try:
        delivery_id = int(request.match_info["id"])
    except ValueError:
        return err("bad_request", "Некорректный номер")
    async with Session() as s:
        d, lead = await lead_actions.own_delivery(s, user, delivery_id)
        if d is None:
            return err("not_found", "Заявка не найдена", 404)
        client_id = None
        if action == "take":
            client_id = (await lead_actions.take(s, user, d, lead)).id
            note = "Клиент в CRM"
        else:
            try:
                note = await lead_actions.apply_status(s, user, d, lead, action)
            except lead_actions.ActionError:
                return err("forbidden", "Действие недоступно", 403)
        await s.commit()
        status = d.status
    return web.json_response({"ok": True, "note": note, "status": status, "client_id": client_id})


async def lead_reply(request: web.Request) -> web.Response:
    """Готовый текст ответа: ?kind=template (шаблон с вашими ценами) или draft (черновик по правилам, бесплатно)."""
    user: User = request[USER_KEY]
    kind = request.query.get("kind", "template")
    if kind not in ("template", "draft"):
        return err("bad_kind", "Неизвестный вид ответа")
    try:
        delivery_id = int(request.match_info["id"])
    except ValueError:
        return err("bad_request", "Некорректный номер")
    async with Session() as s:
        d, lead = await lead_actions.own_delivery(s, user, delivery_id)
        if d is None:
            return err("not_found", "Заявка не найдена", 404)
        text = await (lead_actions.template_text if kind == "template" else lead_actions.rule_draft)(s, user, lead)
    return web.json_response({"text": text, "contact": lead_actions.contact_url(lead)})


async def profile_get(request: web.Request) -> web.Response:
    user: User = request[USER_KEY]
    async with Session() as s:
        return web.json_response(await profile_edit.get_profile(s, user))


async def profile_put(request: web.Request) -> web.Response:
    user: User = request[USER_KEY]
    data = await read_json(request)
    if data is None:
        return err("bad_request", "Некорректный запрос")
    async with Session() as s:
        try:
            changed = await profile_edit.update_profile(s, user, data)
        except profile_edit.ValidationError as e:
            return err("invalid", str(e), 422)
        profile = await profile_edit.get_profile(s, user)
    return web.json_response({"ok": True, "changed": changed, "profile": profile})


async def autosetup_get(request: web.Request) -> web.Response:
    user: User = request[USER_KEY]
    async with Session() as s:
        row = await s.get(User, user.tg_id)
        return web.json_response({
            "intro": await autosetup.user_intro(s, row),
            "plan": [{"what": k, "value": v} for k, v in await autosetup.user_plan(s, row)],
            "missing": await autosetup.user_missing(s, row),
        })


async def autosetup_post(request: web.Request) -> web.Response:
    user: User = request[USER_KEY]
    async with Session() as s:
        n = await autosetup.user_apply(s, user)
    return web.json_response({"ok": True, "applied": n})
