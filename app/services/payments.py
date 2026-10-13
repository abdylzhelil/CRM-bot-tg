"""Оплата тарифа: пакеты дней, запись платежа, продление 💎, доля пригласившему."""
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import config
from app.models import Payment, User, now
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
