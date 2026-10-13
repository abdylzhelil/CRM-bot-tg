"""Сводка «состояние бота» для администратора — данные для бота (текст) и мини-приложения."""
from datetime import timedelta

from sqlalchemy import func, select

from app import config
from app.models import Delivery, Lead, Payment, ScoutAccount, User, now
from app.services import settings, sysmem


async def collect(s) -> dict:
    day = now() - timedelta(days=1)
    count = lambda model, *w: s.scalar(select(func.count()).select_from(model).where(*w))  # noqa: E731
    users = await count(User, User.onboarded) or 0
    paid = await count(User, User.plan == "full", User.is_admin == False) or 0  # noqa: E712
    leads = await count(Lead, Lead.created_at > day) or 0
    sent = await count(Delivery, Delivery.created_at > day) or 0
    pays = await count(Payment, Payment.created_at > now() - timedelta(days=30)) or 0
    accs = (await s.execute(select(ScoutAccount.status, func.count()).where(ScoutAccount.enabled).group_by(ScoutAccount.status))).all()
    get = lambda k: settings.get(s, k)  # noqa: E731
    prices = [await get(k) for k in ("price_30", "price_90", "price_365")]
    return {
        "users": users, "paid": paid, "leads_day": leads, "sent_day": sent, "payments_30d": pays,
        "scout_paused": await get("scout_paused") == "1", "mode": await get("mode"),
        "accounts": {st: n for st, n in accs}, "prices": prices, "selling": any(p != "0" for p in prices),
        "pay_ready": bool(config.PAYMENT_PROVIDER_TOKEN or (config.YOOKASSA_SHOP_ID and config.YOOKASSA_SECRET_KEY)),
        "refund_days": await get("refund_days"),
        "autorenew": await get("autorenew") == "1", "license": await get("license_enabled") == "1",
        "ads": await get("ad_leads") != "0", "ai": await get("ai_enabled") == "1", "ai_key": bool(config.ANTHROPIC_API_KEY),
        "stats_channel": config.STATS_CHANNEL, "memory": sysmem.read(),
    }
