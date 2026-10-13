"""Автонастройка одной кнопкой: для пользователей (по их тарифу) и для администратора (по боту).

Ничего не затирает: заполняет только то, что пусто, и всегда показывает, что изменит, до применения.
"""
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import config
from app.models import ScoutAccount, User, UserCategory, UserNegative
from app.services import settings
from app.services.distribute import is_paid

START_NEGATIVES = ["бесплатно", "даром", "вакансия", "подработка"]  # такие сообщения — не клиенты
START_QUIET = "23-8"


async def user_plan(s: AsyncSession, user: User) -> list[tuple[str, str]]:
    """Что автонастройка сделает пользователю: [(что, значение)] — только пустые поля."""
    todo = []
    if user.paused:
        todo.append(("Заявки", "снять с паузы"))
    if not user.quiet_hours:
        todo.append(("Режим тишины", START_QUIET + " (🔥 горячие заявки всё равно придут сразу)"))
    have = set((await s.scalars(select(UserNegative.word).where(UserNegative.user_id == user.tg_id))).all())
    new_neg = [w for w in START_NEGATIVES if w not in have]
    if not have and new_neg:
        todo.append(("Минус-слова", ", ".join(new_neg)))
    return todo


async def user_missing(s: AsyncSession, user: User) -> list[str]:
    """Что автонастройка угадать не может — выбрать надо самому."""
    out = []
    niches = await s.scalar(select(func.count()).select_from(UserCategory).where(UserCategory.user_id == user.tg_id))
    if not niches:
        out.append("🎯 выберите хотя бы одну нишу")
    if not user.city:
        out.append("📍 город не выбран — будут заявки со всей России (можно выбрать в 📍 Мой город)")
    if not user.prices:
        out.append("💰 впишите свои цены — они подставятся в шаблон ответа")
    return out


async def user_apply(s: AsyncSession, user: User) -> int:
    row = await s.get(User, user.tg_id)
    plan = await user_plan(s, row)
    row.paused = False
    if not row.quiet_hours:
        row.quiet_hours = START_QUIET
    if any(k == "Минус-слова" for k, _ in plan):
        for w in START_NEGATIVES:
            s.add(UserNegative(user_id=row.tg_id, word=w))
    await s.commit()
    return len(plan)


async def user_intro(s: AsyncSession, user: User) -> str:
    """Строка про тариф: что человеку доступно по настройкам бота."""
    if user.is_admin:
        return "👑 Администратор: заявки приходят все и сразу."
    if is_paid(user):
        return "💎 Полный тариф: заявки приходят сразу и без дневного лимита."
    limit = await settings.get_int(s, "free_daily_limit")
    delay = await settings.get_int(s, "free_delay_min")
    return (f"🆓 Бесплатный тариф: до {limit} заявок в день, с задержкой {delay} мин. "
            "Больше и быстрее — 💎 Тариф, приглашения друзей или каталог групп.")


async def admin_checklist(s: AsyncSession) -> tuple[list[str], list[str]]:
    """Готовность бота к работе: (что в порядке, что исправить)."""
    ok, bad = [], []

    def put(cond: bool, good: str, fix: str) -> None:
        (ok if cond else bad).append(good if cond else fix)

    accs = await s.scalar(select(func.count()).select_from(ScoutAccount).where(ScoutAccount.enabled)) or 0
    put(accs > 0, f"Разведчик: аккаунтов {accs}", "Нет аккаунта-разведчика — ⚙️ Настройки → 👤 Аккаунты-разведчики")
    put(await settings.get(s, "scout_paused") == "0", "Разведчик не на паузе", "Разведчик на паузе — 🔍 Группы → ▶️")
    prices = [await settings.get(s, k) for k in ("price_30", "price_90", "price_365")]
    selling = any(p != "0" for p in prices)
    pay_keys = bool(config.PAYMENT_PROVIDER_TOKEN or (config.YOOKASSA_SHOP_ID and config.YOOKASSA_SECRET_KEY))
    if selling:
        put(pay_keys, "Продажа 💎 включена, ключи оплаты есть", "Цены заданы, но ключей оплаты нет в .env — покупка не пройдёт")
    else:
        ok.append("Продажа 💎 выключена (цены 0) — так и задумано, пока не готовы принимать оплату")
    put(await settings.get(s, "refund_days") != "0" or not selling, "Срок возврата задан", "Продаёте 💎, но возврат выключен (refund_days=0)")
    ai_on = await settings.get(s, "ai_enabled") == "1"
    put(not ai_on or bool(config.ANTHROPIC_API_KEY), "ИИ выключен или с ключом", "ИИ включён, но нет ANTHROPIC_API_KEY")
    put(bool(config.STATS_CHANNEL), "Канал статистики подключён", "Канал статистики не подключён (необязательно) — STATS_CHANNEL в .env")
    return ok, bad
