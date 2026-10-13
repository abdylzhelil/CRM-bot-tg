"""Оплата тарифа: пакеты дней, запись платежа, продление 💎, доля пригласившему."""
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import config
from app.models import Payment, PendingLink, User, now
from app.services import plans, referrals, settings

PACKAGES = (30, 90, 365)


def enabled() -> bool:
    return bool(config.PAYMENT_PROVIDER_TOKEN)


async def packages(s: AsyncSession) -> list[tuple[int, int]]:
    """[(дней, цена в ₽)] — только те, у которых цена задана."""
    out = [(d, await settings.get_int(s, f"price_{d}")) for d in PACKAGES]
    return [(d, p) for d, p in out if p > 0]


async def price_of(s: AsyncSession, days: int) -> int:
    return dict(await packages(s)).get(days, 0)


async def apply(s: AsyncSession, user_id: int, days: int, amount: int, currency: str,
                charge_id: str, provider_id: str = "") -> tuple[bool, tuple[int, timedelta] | None]:
    """Записать платёж и продлить тариф. (новый ли платёж, доля пригласившему)."""
    if await s.scalar(select(Payment.id).where(Payment.charge_id == charge_id)):
        return False, None  # Telegram прислал повтор
    s.add(Payment(user_id=user_id, days=days, amount=amount, currency=currency, charge_id=charge_id,
                  provider_id=provider_id))
    user = await s.get(User, user_id)
    bought = timedelta(days=days)
    plans.extend(user, bought)
    share = await referrals.share_of_bonus(s, user, bought)
    await s.commit()
    return True, share


def refunds_enabled() -> bool:
    return bool(config.YOOKASSA_SHOP_ID and config.YOOKASSA_SECRET_KEY)


links_enabled = refunds_enabled  # те же ключи API


async def _yookassa_refund(payment_id: str, amount_kop: int, currency: str, key: str) -> dict:
    """Запрос на возврат в ЮKassa. Повтор с тем же key безопасен (идемпотентность)."""
    import aiohttp

    body = {"payment_id": payment_id, "amount": {"value": f"{amount_kop / 100:.2f}", "currency": currency}}
    auth = aiohttp.BasicAuth(config.YOOKASSA_SHOP_ID, config.YOOKASSA_SECRET_KEY)
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as http:
        async with http.post("https://api.yookassa.ru/v3/refunds", json=body, auth=auth,
                             headers={"Idempotence-Key": key}) as resp:
            data = await resp.json()
            data["_http"] = resp.status
            return data


class RefundError(Exception):
    """Понятная администратору причина."""


async def take_back(s: AsyncSession, payment: Payment) -> None:
    """Снять дни этого платежа с тарифа пользователя (деньги здесь не трогаем)."""
    user = await s.get(User, payment.user_id)
    if user is not None and not user.is_admin and user.plan_until is not None:
        user.plan_until = user.plan_until - timedelta(days=payment.days)
        if user.plan_until <= now():
            user.plan, user.plan_until = "free", None
    payment.refunded, payment.refunded_at = True, now()
    if user is not None:
        disable_renew(user)  # вернули деньги — карту больше не списываем
    await s.commit()


async def refund(s: AsyncSession, payment: Payment, money: bool, call=None) -> str:
    """Вернуть деньги (money=True) и снять тариф. Возвращает текст результата для администратора."""
    if payment.refunded:
        raise RefundError("Этот платёж уже возвращён.")
    note = "Деньги верните вручную в кабинете ЮKassa → Платежи."
    if money:
        if not refunds_enabled():
            raise RefundError("Ключи ЮKassa не заданы (YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY в .env).")
        if not payment.provider_id:
            raise RefundError("У этого платежа нет номера ЮKassa (он сделан до обновления). Верните вручную в кабинете.")
        try:
            data = await (call or _yookassa_refund)(payment.provider_id, payment.amount, payment.currency,
                                                    f"refund-{payment.id}")
        except Exception:
            raise RefundError("ЮKassa не ответила. Попробуйте позже или верните вручную в кабинете.") from None
        if data.get("status") not in ("succeeded", "pending"):
            raise RefundError(f"ЮKassa отказала: {data.get('description') or data.get('status') or data.get('_http')}")
        note = "Деньги возвращены через ЮKassa."
    await take_back(s, payment)
    return note


# ---------- оплата по ссылке (СБП и карта на странице ЮKassa) ----------
API = "https://api.yookassa.ru/v3"


async def _api(method: str, path: str, body: dict | None = None, key: str = "") -> dict:
    import aiohttp

    auth = aiohttp.BasicAuth(config.YOOKASSA_SHOP_ID, config.YOOKASSA_SECRET_KEY)
    headers = {"Idempotence-Key": key} if key else {}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as http:
        async with http.request(method, API + path, json=body, auth=auth, headers=headers) as resp:
            data = await resp.json()
            data["_http"] = resp.status
            return data


async def create_link(s: AsyncSession, user_id: int, days: int, return_url: str, api=None, auto: bool = False) -> str:
    """Создать платёж ЮKassa и вернуть ссылку на оплату. Способ оплаты не задаём — выбор (СБП, карта…) на странице ЮKassa."""
    import uuid

    price = await price_of(s, days)
    if not price:
        raise RefundError("Этот пакет сейчас недоступен.")
    body = {
        "amount": {"value": f"{price:.2f}", "currency": config.PAYMENT_CURRENCY},
        "capture": True,  # одностадийная оплата — иначе СБП не покажется
        "confirmation": {"type": "redirect", "return_url": return_url},
        "description": f"Полный тариф на {days} дн. (Telegram {user_id})",
        "metadata": {"user_id": str(user_id), "days": str(days)},
    }
    if auto:
        body["save_payment_method"] = True  # ЮKassa запомнит карту для списаний за продление (с согласия человека)
    try:
        data = await (api or _api)("POST", "/payments", body, str(uuid.uuid4()))
    except Exception:
        raise RefundError("ЮKassa не ответила. Попробуйте позже.") from None
    url = (data.get("confirmation") or {}).get("confirmation_url")
    if not url or not data.get("id"):
        raise RefundError(f"ЮKassa отказала: {data.get('description') or data.get('_http')}")
    s.add(PendingLink(yk_id=data["id"], user_id=user_id, days=days, amount=price * 100, auto=auto))
    await s.commit()
    return url


async def check_links(s: AsyncSession, api=None, max_age_h: int = 3) -> list[tuple[PendingLink, dict]]:
    """Опросить ЮKassa по ожидающим платежам; вернуть оплаченные (ещё не обработанные)."""
    rows = (await s.scalars(select(PendingLink).where(~PendingLink.done))).all()
    paid = []
    for row in rows:
        if row.created_at < now() - timedelta(hours=max_age_h):
            row.done = True
            continue
        try:
            data = await (api or _api)("GET", f"/payments/{row.yk_id}")
        except Exception:
            continue
        status = data.get("status")
        if status == "succeeded" and data.get("paid", True):
            paid.append((row, data))
        elif status == "canceled":
            row.done = True
    await s.commit()
    return paid


# ---------- автопродление ----------
def autorenew_on(s_value: str) -> bool:
    return s_value == "1" and links_enabled()


def disable_renew(user: User) -> None:
    user.renew_method, user.renew_days, user.renew_for = "", 0, None


async def renew_due(s: AsyncSession, api=None, notify_user=None, notify_admin=None) -> int:
    """Продлить тем, у кого включено автопродление и тариф кончается в ближайшие сутки.

    Одна попытка на каждый срок окончания (renew_for). Не вышло — автопродление выключается, человеку сообщаем.
    Возвращает число успешных продлений.
    """
    import uuid  # noqa: F401  (ключ идемпотентности детерминированный — на случай повтора запроса)

    if await settings.get(s, "autorenew") != "1" or not links_enabled():
        return 0
    soon = now() + timedelta(days=1)
    users = (await s.scalars(select(User).where(User.renew_method != "", User.plan_until.is_not(None),
                                               User.plan_until < soon, ~User.is_admin))).all()
    done = 0
    for u in users:
        if u.renew_for == u.plan_until:
            continue
        u.renew_for = u.plan_until
        days = u.renew_days
        price = await price_of(s, days)
        ok, data = False, {}
        if price:
            body = {
                "amount": {"value": f"{price:.2f}", "currency": config.PAYMENT_CURRENCY}, "capture": True,
                "payment_method_id": u.renew_method,
                "description": f"Автопродление: тариф на {days} дн. (Telegram {u.tg_id})",
                "metadata": {"user_id": str(u.tg_id), "days": str(days), "renew": "1"},
            }
            try:
                data = await (api or _api)("POST", "/payments", body, f"renew-{u.tg_id}-{u.plan_until:%Y%m%d%H%M}")
                ok = data.get("status") == "succeeded"
            except Exception:
                ok = False
        if ok:
            new, share = await apply(s, u.tg_id, days, price * 100, config.PAYMENT_CURRENCY, f"yk:{data['id']}", data["id"])
            done += 1
            if notify_user:
                await notify_user(u.tg_id, f"🔁 Тариф продлён автоматически на {days} дн. (списано {price} ₽). "
                                  "Отключить автопродление: 👤 Профиль → 💎 Тариф.", True)
            if notify_admin:
                await notify_admin(u, days, price * 100, data["id"], share)
        else:
            disable_renew(u)
            await s.commit()
            if notify_user:
                await notify_user(u.tg_id, "⚠️ Не удалось продлить тариф автоматически (платёж не прошёл). "
                                  "Автопродление отключено — продлите вручную в 👤 Профиль → 💎 Тариф.", False)
    await s.commit()
    return done
