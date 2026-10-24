"""💰 Кредиты — внутренняя валюта КлиентРадара (решение владельца 25.10). 1 кредит = 1 ₽ внутри бота.

Откуда: комиссия админу группы — `board_percent`% от СУММЫ оплаты человека, пришедшего по кнопке из закрепа
(boards.reward), и ручное начисление владельцем. Куда: дни 💎 по цене пакетов (price_30/90/365) и коды активации
тарифа бота модерации (владелец создаёт их там командой /newcode и добавляет сюда — сам бот модерации не меняем).
Вывода денег нет: выплаты живыми деньгами — позже и только самозанятым (с чеком), иначе владелец — налоговый агент.

Каждое движение — строка `credit_tx` (видно в «💰 Мои кредиты» и у админа). Возврат оплаты — комиссию списываем
обратно (сколько есть на балансе). Коммит — на вызывающем.
"""
import re
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CreditTx, ModCode, User, now
from app.services import plans, settings


class CreditError(Exception):
    pass


DAY_PACKS = ((30, "price_30"), (90, "price_90"), (365, "price_365"))
MOD_KINDS = {  # тариф бота модерации → (название, настройка цены в кредитах)
    "monthly": ("🛡 Бот модерации — 30 дней", "mod_price_monthly"),
    "yearly": ("🛡 Бот модерации — год", "mod_price_yearly"),
    "forever": ("🛡 Бот модерации — навсегда", "mod_price_forever"),
}
KIND_NAMES = {"board": "комиссия за группу", "admin": "от владельца", "days": "дни 💎", "modcode": "бот модерации",
              "return": "возврат", "reverse": "оплату вернули"}
_CODE = re.compile(r"\b[A-Za-z0-9]{3,}(?:-[A-Za-z0-9]{3,})+\b")


async def enabled(s: AsyncSession) -> bool:
    return await settings.get(s, "credits_enabled") == "1"


async def add(s: AsyncSession, user: User, amount: int, kind: str, note: str = "", ref: str = "") -> CreditTx | None:
    """Начислить (+) или списать (−). С `ref` — не больше одного раза. Баланс в минус не уходит."""
    if amount == 0:
        return None
    if ref and await s.scalar(select(CreditTx.id).where(CreditTx.ref == ref).limit(1)):
        return None
    if amount < 0 and (user.credits or 0) + amount < 0:
        raise CreditError(f"Не хватает кредитов: нужно {-amount}, на счёте {user.credits or 0}.")
    user.credits = (user.credits or 0) + amount
    tx = CreditTx(user_id=user.tg_id, amount=amount, kind=kind, note=note[:200], ref=ref[:64])
    s.add(tx)
    return tx


async def history(s: AsyncSession, user_id: int, limit: int = 10) -> list[CreditTx]:
    return list((await s.scalars(select(CreditTx).where(CreditTx.user_id == user_id)
                                 .order_by(CreditTx.id.desc()).limit(limit))).all())


def tx_line(tx: CreditTx) -> str:
    sign = "+" if tx.amount > 0 else "−"
    what = KIND_NAMES.get(tx.kind, tx.kind)
    return f"{(tx.created_at + timedelta(hours=3)):%d.%m} {sign}{abs(tx.amount)} — {what}" + (f": {tx.note}" if tx.note else "")


# ---------- дни 💎 ----------
async def day_offers(s: AsyncSession) -> list[tuple[int, int]]:
    """[(дней, цена в кредитах)] — только пакеты, которые продаются (цена > 0)."""
    out = []
    for days, key in DAY_PACKS:
        price = await settings.get_int(s, key)
        if price > 0:
            out.append((days, price))
    return out


async def buy_days(s: AsyncSession, user: User, days: int) -> int:
    """Купить дни 💎 за кредиты. Возвращает списанную сумму."""
    price = dict(await day_offers(s)).get(days)
    if not price:
        raise CreditError("Этот пакет сейчас не продаётся.")
    await add(s, user, -price, "days", f"{days} дн.")
    plans.extend(user, timedelta(days=days))
    return price


# ---------- коды бота модерации ----------
async def mod_offers(s: AsyncSession) -> list[tuple[str, str, int, int]]:
    """[(тариф, название, цена, кодов в запасе)] — с ценой > 0."""
    out = []
    for kind, (title, key) in MOD_KINDS.items():
        price = await settings.get_int(s, key)
        if price > 0:
            out.append((kind, title, price, await in_stock(s, kind)))
    return out


async def in_stock(s: AsyncSession, kind: str) -> int:
    return await s.scalar(select(func.count()).select_from(ModCode).where(
        ModCode.kind == kind, ModCode.user_id == 0, ModCode.code != "")) or 0


async def buy_mod(s: AsyncSession, user: User, kind: str) -> ModCode:
    """Купить код бота модерации. Код есть — сразу выдаём; нет — заявка ждёт (code=""), кредиты уже списаны."""
    if kind not in MOD_KINDS:
        raise CreditError("Такого тарифа нет.")
    price = await settings.get_int(s, MOD_KINDS[kind][1])
    if price <= 0:
        raise CreditError("Этот тариф за кредиты сейчас не продаётся.")
    await add(s, user, -price, "modcode", MOD_KINDS[kind][0].replace("🛡 ", ""))
    row = await s.scalar(select(ModCode).where(ModCode.kind == kind, ModCode.user_id == 0, ModCode.code != "")
                         .order_by(ModCode.id).limit(1))
    if row is None:
        row = ModCode(kind=kind, code="")
        s.add(row)
    row.user_id, row.price, row.sold_at = user.tg_id, price, now()
    await s.flush()
    return row


def parse_codes(text: str) -> list[str]:
    found = [c.upper() for c in _CODE.findall(text or "")]
    if not found:  # коды без дефисов — по словам
        found = [w.upper() for w in re.split(r"[\s,;]+", text or "") if 6 <= len(w) <= 64 and w.isalnum()]
    return list(dict.fromkeys(found))


async def add_codes(s: AsyncSession, kind: str, text: str) -> tuple[int, list[ModCode]]:
    """Добавить коды в запас. Сначала закрываем ждущие заявки (их возвращаем, чтобы написать людям).
    → (сколько легло в запас, закрытые заявки)."""
    codes = parse_codes(text)
    known = set((await s.scalars(select(ModCode.code).where(ModCode.code.in_(codes)))).all()) if codes else set()
    codes = [c for c in codes if c not in known]
    waiting = list((await s.scalars(select(ModCode).where(ModCode.kind == kind, ModCode.code == "", ModCode.user_id != 0)
                                    .order_by(ModCode.id))).all())
    filled = []
    for code in codes:
        if waiting:
            row = waiting.pop(0)
            row.code = code
            filled.append(row)
        else:
            s.add(ModCode(kind=kind, code=code))
    await s.flush()
    return len(codes) - len(filled), filled


async def cancel_waiting(s: AsyncSession, row: ModCode) -> int:
    """Отменить ждущую заявку на код и вернуть кредиты. → сколько вернули."""
    if row.code or not row.user_id:
        raise CreditError("Код уже выдан — отменить нельзя.")
    user = await s.get(User, row.user_id)
    back = row.price
    if user is not None and back:
        await add(s, user, back, "return", "заявка на код бота модерации отменена")
    await s.delete(row)
    return back


async def waiting_of(s: AsyncSession, user_id: int = 0) -> list[ModCode]:
    q = select(ModCode).where(ModCode.code == "", ModCode.user_id != 0)
    if user_id:
        q = q.where(ModCode.user_id == user_id)
    return list((await s.scalars(q.order_by(ModCode.id))).all())


async def codes_of(s: AsyncSession, user_id: int) -> list[ModCode]:
    return list((await s.scalars(select(ModCode).where(ModCode.user_id == user_id, ModCode.code != "")
                                 .order_by(ModCode.id.desc()).limit(10))).all())


async def stock(s: AsyncSession) -> dict[str, tuple[int, int]]:
    """{тариф: (в запасе, ждут кода)}."""
    out = {}
    for kind in MOD_KINDS:
        waiting = await s.scalar(select(func.count()).select_from(ModCode).where(
            ModCode.kind == kind, ModCode.code == "", ModCode.user_id != 0)) or 0
        out[kind] = (await in_stock(s, kind), waiting)
    return out


async def unsold(s: AsyncSession, kind: str) -> list[ModCode]:
    return list((await s.scalars(select(ModCode).where(ModCode.kind == kind, ModCode.user_id == 0, ModCode.code != "")
                                 .order_by(ModCode.id))).all())


# ---------- комиссия и возврат ----------
def commission(amount_kop: int, percent: int) -> int:
    """Кредиты с оплаты: percent% суммы в рублях (вниз до целого)."""
    return max(0, amount_kop // 100 * percent // 100)


async def reverse_payment(s: AsyncSession, charge_id: str) -> int:
    """Оплату вернули — забрать начисленную с неё комиссию (сколько есть на балансе). → списано."""
    if not charge_id:
        return 0
    taken = 0
    for tx in (await s.scalars(select(CreditTx).where(CreditTx.ref == f"pay-{charge_id}"))).all():
        if await s.scalar(select(CreditTx.id).where(CreditTx.ref == f"rev-{tx.id}").limit(1)):
            continue
        user = await s.get(User, tx.user_id)
        if user is None:
            continue
        amount = min(tx.amount, user.credits or 0)
        user.credits = (user.credits or 0) - amount
        s.add(CreditTx(user_id=user.tg_id, amount=-amount, kind="reverse", note="оплату приглашённого вернули",
                       ref=f"rev-{tx.id}"))
        taken += amount
    return taken


async def totals(s: AsyncSession) -> tuple[int, int, int]:
    """(на счетах всего, у скольких людей, начислено за всё время)."""
    total = await s.scalar(select(func.coalesce(func.sum(User.credits), 0))) or 0
    holders = await s.scalar(select(func.count()).select_from(User).where(User.credits > 0)) or 0
    earned = await s.scalar(select(func.coalesce(func.sum(CreditTx.amount), 0)).where(
        CreditTx.kind.in_(("board", "admin")))) or 0
    return int(total), holders, int(earned)


async def top(s: AsyncSession, limit: int = 10) -> list[User]:
    return list((await s.scalars(select(User).where(User.credits > 0).order_by(User.credits.desc()).limit(limit))).all())
