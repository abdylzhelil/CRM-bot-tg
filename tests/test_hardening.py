"""Защита от злоупотреблений, бэкап, напоминание о конце тарифа, условия оплаты."""
import sqlite3
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy import select

from app.bot.handlers import payments as ph
from app.main import expiry_once
from app.models import User, now
from app.services import backup, plans, referrals, settings
from tests.conftest import ADMIN_ID, make_user


def test_bonus_cap():
    u = User(tg_id=5, plan="free")
    assert plans.extend(u, timedelta(days=100), timedelta(days=180))
    assert plans.extend(u, timedelta(days=100), timedelta(days=180))  # дотянули до потолка
    assert timedelta(days=179) < u.plan_until - now() <= timedelta(days=180)
    assert not plans.extend(u, timedelta(days=10), timedelta(days=180))  # дальше потолка не растёт
    assert plans.extend(u, timedelta(days=30))  # оплата потолком не ограничена
    assert u.plan_until - now() > timedelta(days=200)


async def test_referral_limit(maker, tgbot):
    async with maker() as s:
        await settings.put(s, "ref_limit", "2")
    await make_user(maker, 10)
    results = []
    for uid in (21, 22, 23):
        await make_user(maker, uid, referred_by=10)
        async with maker() as s:
            results.append(await referrals.reward_signup(s, await s.get(User, uid)))
    assert results == [10, 10, None]  # третий приглашённый наград не приносит
    async with maker() as s:
        assert (await s.get(User, 23)).plan == "free"


def test_backup_keeps_last_seven(tmp_path):
    db = tmp_path / "crm.db"
    con = sqlite3.connect(db)
    con.execute("create table t (x int)")
    con.execute("insert into t values (42)")
    con.commit()
    con.close()
    folder = tmp_path / "backups"
    folder.mkdir()
    for i in range(10):
        (folder / f"crm-2020-01-{i + 1:02d}.db").write_text("old")
    dest = backup.make_backup(db, folder)
    assert sqlite3.connect(dest).execute("select x from t").fetchone() == (42,)
    assert len(list(folder.glob("crm-*.db"))) == 7


async def test_expiry_notice_once(maker, tgbot):
    await make_user(maker, 40, plan="full", plan_until=now() + timedelta(days=2))
    await make_user(maker, 41, plan="full", plan_until=now() + timedelta(days=20))
    await make_user(maker, 42, plan="full", plan_until=now() - timedelta(days=1))
    assert await expiry_once() == 1
    assert await expiry_once() == 0  # повторно не напоминаем
    assert any("заканчивается" in t for t in tgbot.texts(40)) and not tgbot.texts(41) and not tgbot.texts(42)
    async with maker() as s:  # продлил — на новый срок напомним снова
        (await s.get(User, 40)).plan_until = now() + timedelta(days=1)
        await s.commit()
    assert await expiry_once() == 1


async def test_terms_and_paysupport(maker, tgbot):
    async with maker() as s:
        (await s.get(User, ADMIN_ID)).username = "owner_nick"
        await settings.put(s, "price_30", "990")
    for handler in (ph.terms, ph.paysupport):
        m = MagicMock()
        m.from_user.id = 77
        m.answer = AsyncMock()
        await handler(m)
        text = m.answer.call_args.args[0]
        assert "@owner_nick" in text
    assert "990" in (await _terms_text(maker))


async def _terms_text(maker):
    m = MagicMock()
    m.answer = AsyncMock()
    await ph.terms(m)
    return m.answer.call_args.args[0]
