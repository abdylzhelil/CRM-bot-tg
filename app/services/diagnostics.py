"""«🩺 Отчёт о проблемах»: сводка за неделю — сбои, ошибки фильтра, реклама, разведчики, оплаты.

Владелец пересылает этот файл разработчику (в чат с Claude Code) — по нему видно, что чинить и как подкрутить правила.
Без имён, ников и телефонов людей: только числа, причины и (обезличенные) примеры ошибок фильтра.
"""
import re
from collections import Counter
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AdOrder, AdPost, Delivery, ErrorLog, FilteredMessage, Group, LabeledText, Lead, Payment, PendingLink, ScoutAccount,
    SpamSample, User, now,
)

_PRIVATE = re.compile(r"(@\w{3,}|\+?\d[\d\s\-()]{8,}\d|https?://\S+|t\.me/\S+)")


def _anon(text: str, n: int = 160) -> str:
    return _PRIVATE.sub("…", " ".join((text or "").split()))[:n]


async def _count(s, model, *where) -> int:
    return await s.scalar(select(func.count()).select_from(model).where(*where)) or 0


async def report(s: AsyncSession, days: int = 7) -> str:
    since = now() - timedelta(days=days)
    out = [f"🩺 ОТЧЁТ О ПРОБЛЕМАХ за {days} дн. (сформирован {now():%d.%m.%Y %H:%M} UTC)", ""]

    # 1. сбои
    errs = (await s.execute(select(ErrorLog.key, func.count(), func.max(ErrorLog.text)).where(ErrorLog.created_at >= since)
                            .group_by(ErrorLog.key).order_by(func.count().desc()).limit(15))).all()
    out.append(f"1) СБОИ (тревоги 🚨): {sum(r[1] for r in errs)}")
    out += [f"   • {k}: {n} раз — {_anon(t, 200)}" for k, n, t in errs] or ["   нет — хорошо"]

    # 2. заявки и фильтр
    leads = await _count(s, Lead, Lead.created_at >= since)
    st = dict((await s.execute(select(Delivery.status, func.count()).where(Delivery.created_at >= since, Delivery.sent)
                               .group_by(Delivery.status))).all())
    filtered = await _count(s, FilteredMessage, FilteredMessage.created_at >= since)
    marked_offer = await _count(s, LabeledText, LabeledText.kind == "offer", LabeledText.created_at >= since)
    restored = await _count(s, LabeledText, LabeledText.kind == "request", LabeledText.created_at >= since)
    hits = await s.scalar(select(func.coalesce(func.sum(SpamSample.hits), 0))) or 0
    out += ["", f"2) ЗАЯВКИ: {leads}; доставки — " + ", ".join(f"{k} {v}" for k, v in sorted(st.items())),
            f"   отсеяно как реклама услуг: {filtered} (за 3 дня хранится); ошибочно отсеяно и возвращено: {restored}",
            f"   пропущено фильтром (вы нажали «🙅 Исполнитель»): {marked_offer}; спам-текстов отсеяно всего: {hits}"]
    total_marks = restored + marked_offer
    if leads:
        out.append(f"   ≈ точность фильтра: {100 - round(100 * total_marks / max(leads + filtered, 1))}% "
                   "(чем больше ваших исправлений, тем ниже)")
    ex = (await s.execute(select(LabeledText.kind, LabeledText.text).where(LabeledText.created_at >= since)
                          .order_by(LabeledText.id.desc()).limit(12))).all()
    if ex:
        out.append("   Примеры ваших исправлений (для подкрутки правил):")
        out += [f"   [{'исполнитель' if k == 'offer' else 'заказчик'}] {_anon(t)}" for k, t in ex]

    # 3. реклама в группах
    orders = dict((await s.execute(select(AdOrder.status, func.count()).where(AdOrder.created_at >= since)
                                   .group_by(AdOrder.status))).all())
    posts = dict((await s.execute(select(AdPost.status, func.count()).join(AdOrder, AdOrder.id == AdPost.order_id)
                                  .where(AdOrder.created_at >= since).group_by(AdPost.status))).all())
    reasons = Counter((await s.scalars(select(AdPost.error).join(AdOrder, AdOrder.id == AdPost.order_id).where(
        AdOrder.created_at >= since, AdPost.status.in_(("failed", "deleted"))))).all())
    out += ["", "3) РЕКЛАМА: заказы — " + (", ".join(f"{k} {v}" for k, v in sorted(orders.items())) or "нет"),
            "   посты — " + (", ".join(f"{k} {v}" for k, v in sorted(posts.items())) or "нет")]
    out += [f"   • не вышло: {r or 'без причины'} — {n}" for r, n in reasons.most_common(8)]
    allowed = await _count(s, Group, Group.ads_allowed)
    closed = await _count(s, Group, Group.ads_allowed, Group.ad_closed_at.is_not(None))
    unchecked = await _count(s, Group, Group.ads_allowed, Group.ad_checked_at.is_(None))
    out.append(f"   групп для рекламы: {allowed}, закрыто сейчас: {closed}, ещё не проверено: {unchecked}")

    # 4. разведчики и группы
    accs = (await s.execute(select(ScoutAccount.name, ScoutAccount.status, ScoutAccount.enabled))).all()
    gst = dict((await s.execute(select(Group.status, func.count()).group_by(Group.status))).all())
    out += ["", "4) РАЗВЕДЧИКИ: " + (", ".join(f"#{i + 1} {st}{'' if en else ' (выкл)'}" for i, (_, st, en) in enumerate(accs)) or "нет"),
            "   группы — " + ", ".join(f"{k} {v}" for k, v in sorted(gst.items()))]

    # 5. деньги и люди
    pays = await _count(s, Payment, Payment.created_at >= since)
    refunded = await _count(s, Payment, Payment.created_at >= since, Payment.refunded_amount > 0)
    stuck = await _count(s, PendingLink, ~PendingLink.done)
    new_users = await _count(s, User, User.created_at >= since)
    unfinished = await _count(s, User, User.created_at >= since, ~User.onboarded)
    out += ["", f"5) ОПЛАТЫ: {pays}, с возвратом: {refunded}, ссылок ждут оплаты: {stuck}",
            f"   ЛЮДИ: новых {new_users}, не закончили регистрацию {unfinished}"]

    # 6. подсказки
    tips = []
    if errs:
        tips.append("есть сбои — пришлите этот отчёт разработчику")
    if restored >= 3:
        tips.append("фильтр часто отсеивает заказчиков — правила надо смягчить")
    if marked_offer >= 3:
        tips.append("фильтр пропускает рекламу услуг — правила надо усилить")
    if unchecked:
        tips.append("есть непроверенные группы для рекламы — 🔍 Группы → 📢 → «🔎 Проверить все разрешённые»")
    if reasons:
        tips.append("посты не выходят — посмотрите причины в пункте 3")
    out += ["", "6) ЧТО СДЕЛАТЬ: " + ("; ".join(tips) if tips else "явных проблем нет 👍")]
    return "\n".join(out)
