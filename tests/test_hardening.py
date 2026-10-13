"""Защита от злоупотреблений, бэкап, напоминание о конце тарифа, условия оплаты."""
import sqlite3
from datetime import timedelta
from types import SimpleNamespace
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


# ---------- проверка «я не бот» и чистка пустых аккаунтов ----------
async def test_referral_survives_captcha(maker, tgbot):
    from app.bot import captcha

    await make_user(maker, 10)
    async with maker() as s:
        s.add(User(tg_id=80, verified=False))
        await s.commit()
    ev = MagicMock()
    ev.text = "/start ref_10"
    ev.answer = AsyncMock()
    user = SimpleNamespace(tg_id=80, verified=False, is_admin=False, onboarded=False, name="x", username="")
    assert await captcha.gate(ev, AsyncMock(), {}, user) is None  # проверку показали, обработчик не вызван
    icons, target, param = captcha._pending[80]
    assert param == "ref_10"
    cb = MagicMock()
    cb.data = f"cap:{icons.index(target)}"
    cb.answer, cb.message.delete, cb.message.answer = AsyncMock(), AsyncMock(), AsyncMock()
    await captcha.answer(cb, user)
    cb.message.answer.assert_called()  # дальше человек видит выбор ниш
    async with maker() as s:
        u = await s.get(User, 80)
    assert u.verified and u.referred_by == 10  # приглашение не потерялось


async def test_clean_empty_accounts(maker):
    from app.bot.handlers import users_admin

    await make_user(maker, 90)  # настоящий пользователь
    async with maker() as s:
        s.add(User(tg_id=91, onboarded=False, created_at=now() - timedelta(days=5)))  # давно зашёл и ушёл
        s.add(User(tg_id=92, onboarded=False, created_at=now() - timedelta(hours=2)))  # только зашёл
        await s.commit()
    async with maker() as s:
        assert await users_admin.empty_users(s) == [91]
    c = MagicMock()
    c.data = "us:clean:do"
    c.answer, c.message.edit_text, c.message.answer = AsyncMock(), AsyncMock(), AsyncMock()
    await users_admin.clean_do(c)
    async with maker() as s:
        assert await s.get(User, 91) is None and await s.get(User, 92) and await s.get(User, 90)


async def test_warning_then_delete_after_seven_days(maker):
    from app.models import UserCategory
    from app.services import cleanup

    await make_user(maker, 95)  # настоящий
    async with maker() as s:
        s.add(User(tg_id=96, onboarded=False, created_at=now() - timedelta(days=8)))  # давно, ещё не предупреждён
        s.add(User(tg_id=97, onboarded=False, created_at=now() - timedelta(days=3)))  # рано
        s.add(User(tg_id=98, onboarded=False, created_at=now() - timedelta(days=30), is_admin=True))  # админа не трогаем
        s.add(User(tg_id=99, onboarded=False, created_at=now() - timedelta(days=8), blocked=True))  # бот заблокирован
        s.add(UserCategory(user_id=96, category_id=1))  # выбрал нишу, но не дошёл до конца — хвосты тоже уходят
        await s.commit()
    async with maker() as s:  # ещё не предупредили — не удаляем (кроме заблокировавших бота)
        assert await cleanup.auto_cleanup(s) == 1
    sent = []

    async def send(uid, text, markup=None):
        sent.append((uid, text, markup))
        return True

    async with maker() as s:
        assert await cleanup.warn_due(s, send) == 1  # предупреждён один: 96 (за 1 день до срока в 7 дней)
        assert await cleanup.warn_due(s, send) == 0  # повторно не пишем
    assert sent[0][0] == 96 and "удалим" in sent[0][1] and "reg:go" in str(sent[0][2])
    async with maker() as s:
        assert await cleanup.auto_cleanup(s) == 0  # предупреждение только что — срок «отлежать» не прошёл
        (await s.get(User, 96)).warned_at = now() - timedelta(days=2)
        await s.commit()
        assert await cleanup.auto_cleanup(s) == 1
    async with maker() as s:
        assert await s.get(User, 96) is None and await s.get(UserCategory, (96, 1)) is None
        assert await s.get(User, 97) and await s.get(User, 98) and await s.get(User, 95)
        await settings.put(s, "cleanup_days", "0")  # 0 — автоудаление выключено
        s.add(User(tg_id=100, onboarded=False, created_at=now() - timedelta(days=60)))
        await s.commit()
        assert await cleanup.auto_cleanup(s) == 0 and await cleanup.warn_due(s, send) == 0
        assert await s.get(User, 100)


async def test_warn_marks_blocked_when_undeliverable(maker):
    from app.services import cleanup

    async with maker() as s:
        s.add(User(tg_id=101, onboarded=False, created_at=now() - timedelta(days=7)))
        await s.commit()

    async def refuse(uid, text, markup=None):
        return False

    async with maker() as s:
        assert await cleanup.warn_due(s, refuse) == 0
        assert (await s.get(User, 101)).blocked  # не доставить — значит бот заблокирован, удалим по сроку
        assert await cleanup.auto_cleanup(s) == 1


async def test_registration_screen_mentions_deletion_period(maker, tgbot):
    from app.bot.handlers import profile

    m = MagicMock()
    m.answer = AsyncMock()
    await profile.show_niches(m, SimpleNamespace(tg_id=300, onboarded=False))
    assert "удалится" in m.answer.call_args.args[0] and "7 дн" in m.answer.call_args.args[0]
