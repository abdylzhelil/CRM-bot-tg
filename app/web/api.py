"""Методы кабинета: заявки, профиль, автонастройка. Бизнес-логика — в app/services (та же, что у бота)."""
from aiohttp import web
from sqlalchemy import select

from app.db import Session
from app.models import STAGE_NAMES, Delivery, Group, Lead, User, UserCategory
from app.services import (
    ads, autosetup, catalog, crm, lead_actions, levels, payments, profile_edit, quiet, referrals, settings,
)
from app.services.distribute import is_paid
from app.bot.handlers.leads import category_name
from app.web.keys import USER_KEY

STATUSES = ("new", "postponed", "taken", "rejected", "spam", "muted")  # muted — «🔕 накопилось без уведомлений»
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
        before = _bounded(request.query.get("before", "0"))
    except ValueError:
        return err("bad_request", "Некорректный запрос")
    async with Session() as s:
        q = (select(Delivery, Lead).join(Lead, Lead.id == Delivery.lead_id)
             .where(Delivery.user_id == user.tg_id, Delivery.sent))
        q = q.where(Delivery.muted, Delivery.status == "new") if status == "muted" else q.where(Delivery.status == status)
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
        delivery_id = _bounded(request.match_info["id"])
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
        delivery_id = _bounded(request.match_info["id"])
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


# ---------- CRM ----------
async def client_item(s, c) -> dict:
    return {
        "id": c.id, "name": c.name, "username": c.username, "phone": c.phone, "stage": c.stage, "budget": c.budget,
        "source": c.source, "category": await category_name(s, c.category_id) if c.category_id else "",
        "updated_at": c.updated_at.isoformat(),
    }


async def client_card(s, c) -> dict:
    item = await client_item(s, c)
    rem = await crm.next_reminder(s, c.id)
    item.update({
        "notes": c.notes,
        "reminder": rem.due_at.isoformat() if rem else None,
        "events": [{"text": e.text, "at": e.created_at.isoformat()} for e in await crm.events(s, c.id)],
        "contact": (f"https://t.me/{c.username}" if c.username else (f"tg://user?id={c.tg_id}" if c.tg_id else "")),
    })
    return item


MAX_INT = 2**62  # больше не влезает в SQLite: огромный номер в запросе дал бы ошибку 500 вместо «не найдено»


def _bounded(raw) -> int:
    """int(raw) или ValueError, если это не число или оно не помещается в базу."""
    value = int(raw)
    if abs(value) > MAX_INT:
        raise ValueError(raw)
    return value


def _int(raw, default=0):
    try:
        return _bounded(raw)
    except (TypeError, ValueError):
        return default


async def clients_list(request: web.Request) -> web.Response:
    user: User = request[USER_KEY]
    stage = request.query.get("stage", "all")
    if stage != "all" and stage not in STAGE_NAMES:
        return err("bad_stage", "Неизвестный этап")
    async with Session() as s:
        items, more = await crm.list_clients(s, user.tg_id, stage, request.query.get("q", "").strip()[:64], max(_int(request.query.get("offset")), 0))
        return web.json_response({
            "items": [await client_item(s, c) for c in items], "has_more": more,
            "counts": await crm.counts(s, user.tg_id), "stages": [{"id": k, "title": v} for k, v in STAGE_NAMES.items()],
        })


async def client_get(request: web.Request) -> web.Response:
    user: User = request[USER_KEY]
    async with Session() as s:
        c = await crm.owned(s, _int(request.match_info["id"]), user.tg_id)
        if c is None:
            return err("not_found", "Клиент не найден", 404)
        return web.json_response(await client_card(s, c))


async def client_create(request: web.Request) -> web.Response:
    user: User = request[USER_KEY]
    data = await read_json(request)
    if data is None:
        return err("bad_request", "Некорректный запрос")
    async with Session() as s:
        try:
            c = await crm.create_client(s, user.tg_id, str(data.get("name", "")), str(data.get("username", "")), str(data.get("phone", "")))
        except crm.CrmError as e:
            return err("invalid", str(e), 422)
        return web.json_response(await client_card(s, c), status=201)


async def client_patch(request: web.Request) -> web.Response:
    """Меняет присланное: stage, budget, phone, name, note (дописывается), remind_days (добавляет напоминание),
    remind_off (убирает напоминания)."""
    user: User = request[USER_KEY]
    data = await read_json(request)
    if data is None:
        return err("bad_request", "Некорректный запрос")
    async with Session() as s:
        c = await crm.owned(s, _int(request.match_info["id"]), user.tg_id)
        if c is None:
            return err("not_found", "Клиент не найден", 404)
        try:
            if "stage" in data:
                await crm.set_stage(s, c, str(data["stage"]))
            for field in ("budget", "phone", "note", "name"):
                if field in data and str(data[field]).strip():
                    await crm.update_field(s, c, field, str(data[field]))
            if "remind_days" in data:
                await crm.add_reminder(s, c, _int(data["remind_days"], -1))
            if data.get("remind_off"):
                await crm.cancel_reminders(s, c)
        except crm.CrmError as e:
            return err("invalid", str(e), 422)
        return web.json_response(await client_card(s, c))


async def client_delete(request: web.Request) -> web.Response:
    user: User = request[USER_KEY]
    async with Session() as s:
        c = await crm.owned(s, _int(request.match_info["id"]), user.tg_id)
        if c is None:
            return err("not_found", "Клиент не найден", 404)
        await crm.delete_client(s, c)
    return web.json_response({"ok": True})


# ---------- друзья ----------
async def bot_username() -> str:
    from app.bot import notify

    return (await notify._bot.me()).username


async def referrals_get(request: web.Request) -> web.Response:
    from urllib.parse import quote

    user: User = request[USER_KEY]
    link = referrals.link(await bot_username(), user.tg_id)
    async with Session() as s:
        total, done = await referrals.stats(s, user.tg_id)
        days, percent = await settings.get_int(s, "ref_days"), await settings.get_int(s, "ref_percent")
    share = f"https://t.me/share/url?url={quote(link, safe='')}&text={quote('КлиентРадар — заявки от клиентов из Telegram-групп. Заходи:')}"
    return web.json_response({"link": link, "share": share, "days": days, "percent": percent, "invited": total, "done": done})


# ---------- каталог ----------
def group_item(g, ad_active: bool) -> dict:
    return {"title": g.title, "username": g.username, "kind": g.kind, "members": g.members, "city": g.city,
            "leads": g.leads_count, "ad": ad_active, "link": f"https://t.me/{g.username}" if g.username else ""}


async def catalog_get(request: web.Request) -> web.Response:
    user: User = request[USER_KEY]
    scope = request.query.get("scope", "mine")
    if scope not in ("mine", "all"):
        return err("bad_scope", "Неизвестный раздел каталога")
    async with Session() as s:
        roots = set((await s.scalars(select(UserCategory.category_id).where(UserCategory.user_id == user.tg_id))).all())
        groups = await catalog.browse(s, user, roots, scope == "all")
        groups_n, leads_n = await levels.stats(s, user.tg_id)
        per = await settings.get_int(s, "bonus_leads")
    level = levels.level_of(groups_n, leads_n)
    return web.json_response({
        "groups": [group_item(g, ads.active(g)) for g in groups],
        "level": {"name": levels.name(level), "leads": leads_n, "next": levels.next_step(level, leads_n)},
        "bonus_leads": per,
    })


async def catalog_my(request: web.Request) -> web.Response:
    user: User = request[USER_KEY]
    async with Session() as s:
        groups = (await s.scalars(select(Group).where(Group.submitted_by == user.tg_id, Group.status != "draft")
                                  .order_by(Group.submitted_at.desc()).limit(30))).all()
        per = await settings.get_int(s, "bonus_leads")
        cost = await settings.get_int(s, "ad_leads")
    items = [{
        "id": g.id, "title": g.title, "kind": g.kind, "status": catalog_status(g.status), "in_catalog": bool(g.catalog),
        "leads": g.leads_count, "bonus_days": catalog.bonus_days(g, per), "ad_active": ads.active(g),
        "ad_until": g.ad_until.isoformat() if g.ad_until else None, "ad_pending": g.ad_pending,
        "ad_available": ads.available(g),
    } for g in groups]
    return web.json_response({
        "items": items, "bonus_days": sum(i["bonus_days"] for i in items), "level": levels.name(user.level),
        "ad_cost": cost,
    })


def catalog_status(status: str) -> str:
    from app.bot.handlers.catalog import STATUS

    return STATUS.get(status, status)


async def ad_order(request: web.Request) -> web.Response:
    from app.bot import notify
    from app.bot.handlers.submissions import ad_card

    user: User = request[USER_KEY]
    gid = _int(request.match_info["id"])
    async with Session() as s:
        try:
            g = await ads.request(s, user.tg_id, gid)
        except ads.AdError as e:
            return err("invalid", str(e), 422)
    text, markup = await ad_card(g, user)
    await notify._send("📢 Заказ рекламы:\n\n" + text, markup)
    return web.json_response({"ok": True, "message": "Заявка отправлена администратору"})


async def rating_get(request: web.Request) -> web.Response:
    user: User = request[USER_KEY]
    everything = request.query.get("scope", "all") == "all" or not user.city
    async with Session() as s:
        board = await levels.rating(s, "" if everything else user.city, limit=0)
    place = next((i for i, (u, _) in enumerate(board, 1) if u.tg_id == user.tg_id), None)
    top = [{"place": i, "name": (u.name or "Аноним").split()[0][:20], "level": levels.name(levels.level_of(1, pts)),
            "leads": pts, "me": u.tg_id == user.tg_id} for i, (u, pts) in enumerate(board[:10], 1)]
    return web.json_response({"top": top, "place": place, "scope": "all" if everything else "city", "city": user.city})


# ---------- тариф и оплата ----------
async def plans_get(request: web.Request) -> web.Response:
    from app.bot.handlers.payments import refund_line

    user: User = request[USER_KEY]
    async with Session() as s:
        offers = await payments.packages(s)
        refund_days = await settings.get_int(s, "refund_days")
        row = await s.get(User, user.tg_id)
        line = await autosetup.user_intro(s, row)
    return web.json_response({
        "intro": line, "paid": is_paid(row), "until": row.plan_until.isoformat() if row.plan_until else None,
        "packages": [{"days": d, "price": p} for d, p in offers],
        "can_pay": payments.links_enabled() and bool(offers) and not row.is_admin,
        "refund": refund_line(refund_days),
        "renew": {"card": row.renew_card or "Банковская карта", "days": row.renew_days} if row.renew_method else None,
    })


async def renew_off(request: web.Request) -> web.Response:
    """Отвязать карту: автопродление отключается, списаний больше нет (так же, как в боте)."""
    user: User = request[USER_KEY]
    async with Session() as s:
        row = await s.get(User, user.tg_id)
        if not row.renew_method:
            return err("not_found", "Карта не привязана", 404)
        payments.disable_renew(row)
        await s.commit()
    return web.json_response({"ok": True, "message": "Карта отвязана, автопродление отключено"})


# ---------- 💰 кредиты (та же логика, что в боте: services/credits.py) ----------
async def credits_get(request: web.Request) -> web.Response:
    from app.services import credits

    user: User = request[USER_KEY]
    async with Session() as s:
        row = await s.get(User, user.tg_id)
        hist = await credits.history(s, user.tg_id, 20)
        waiting = await credits.waiting_of(s, user.tg_id)
        codes = await credits.codes_of(s, user.tg_id)
        days = await credits.day_offers(s)
        mods = await credits.mod_offers(s)
        pct = await settings.get_int(s, "board_percent")
        bot_name = (await settings.get(s, "mod_bot_username") or "").lstrip("@")
    name = lambda k: credits.MOD_KINDS.get(k, (k,))[0].replace("🛡 ", "")  # noqa: E731
    return web.json_response({
        "balance": row.credits or 0, "percent": pct, "mod_bot": bot_name,
        "history": [{"line": credits.tx_line(t)} for t in hist],
        "waiting": [{"id": w.id, "title": name(w.kind), "price": w.price} for w in waiting],
        "codes": [{"title": name(c.kind), "code": c.code} for c in codes],
        "days": [{"days": d, "price": p} for d, p in days],
        "mods": [{"kind": k, "title": t.replace("🛡 ", ""), "price": p, "stock": n} for k, t, p, n in mods],
    })


async def credits_buy(request: web.Request) -> web.Response:
    """{"days": 30} — дни 💎; {"mod": "monthly"} — код бота модерации; {"cancel": id} — отменить ждущую заявку."""
    from app.bot.handlers import credits as credits_h
    from app.models import ModCode
    from app.services import credits

    user: User = request[USER_KEY]
    data = await read_json(request) or {}
    async with credits_h._lock, Session() as s:
        row = await s.get(User, user.tg_id)
        try:
            if "days" in data:
                price = await credits.buy_days(s, row, _int(data.get("days"), 0))
                msg = f"Готово: +{_int(data.get('days'), 0)} дн. 💎, списано {price}"
                mc = None
            elif "mod" in data:
                mc = await credits.buy_mod(s, row, str(data.get("mod")))
                msg = "Код готов" if mc.code else "Заявка принята — код придёт в чат с ботом, как только владелец его добавит"
            elif "cancel" in data:
                mc = await s.get(ModCode, _int(data.get("cancel"), 0))
                if mc is None or mc.user_id != user.tg_id:
                    return err("not_found", "Заявка не найдена", 404)
                back = await credits.cancel_waiting(s, mc)
                mc, msg = None, f"Отменено, вернули {back}"
            else:
                return err("invalid", "Что купить?", 422)
        except credits.CreditError as e:
            return err("invalid", str(e), 422)
        await s.commit()
        balance = row.credits
    if mc is not None and not mc.code:
        try:
            from app.bot import notify

            await notify._send(f"🔑 {user.name or user.tg_id} купил код бота модерации за кредиты, а кодов в запасе нет.",
                               credits_h.kb([[("🔑 Добавить коды", f"cra:add:{mc.kind}")]]))
        except Exception:
            pass
    return web.json_response({"ok": True, "message": msg, "balance": balance, "code": mc.code if mc else ""})


async def pay_create(request: web.Request) -> web.Response:
    """Создаёт платёж ЮKassa (СБП или карта) и возвращает ссылку; тариф включится сам после оплаты (как в боте)."""
    user: User = request[USER_KEY]
    data = await read_json(request)
    days = _int((data or {}).get("days"), 0)
    if not payments.links_enabled():
        return err("unavailable", "Оплата пока не подключена", 503)
    async with Session() as s:
        row = await s.get(User, user.tg_id)
        if row.is_admin:
            return err("forbidden", "Администратору оплата не нужна", 403)
        if days not in dict(await payments.packages(s)):
            return err("invalid", "Этот пакет сейчас недоступен", 422)
        try:
            url = await payments.create_link(s, user.tg_id, days, f"https://t.me/{await bot_username()}")
        except payments.RefundError as e:
            return err("payment_failed", str(e), 502)
    return web.json_response({"url": url})


async def stats_get(request: web.Request) -> web.Response:
    from app.bot.handlers.stats import user_stats

    user: User = request[USER_KEY]
    st = await user_stats(user.tg_id)
    st["stages"] = [{"id": k, "title": STAGE_NAMES[k], "count": v} for k, v in st["stages"].items() if k in STAGE_NAMES]
    return web.json_response(st)


async def mute_set(request: web.Request) -> web.Response:
    """🔕 Выключить уведомления о заявках на время ({"code": "3h"}) или включить ({"on": true}). Та же логика, что в боте."""
    from app.services import mute

    user: User = request[USER_KEY]
    data = await read_json(request)
    if data is None:
        return err("bad_request", "Некорректный запрос")
    async with Session() as s:
        if data.get("on"):
            n = await mute.unmute(s, user)
            await s.commit()
            return web.json_response({"ok": True, "on": False, "accumulated": n})
        code = data.get("code")
        if code not in dict(mute.OPTIONS):
            return err("bad_code", "Неизвестный срок")
        await mute.set_mute(s, user, code)
        await s.commit()
        row = await s.get(User, user.tg_id)
        return web.json_response({"ok": True, "on": True, "until": mute.until_text(row)})
