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
    assert await expiry_once() == 2  # 40 — «скоро кончится», 42 — «уже закончился»
    assert await expiry_once() == 0  # повторно не пишем
    assert any("заканчивается" in t for t in tgbot.texts(40)) and not tgbot.texts(41)
    assert any("закончился" in t for t in tgbot.texts(42))
    async with maker() as s:  # продлил — на новый срок напомним снова
        assert (await s.get(User, 42)).plan == "free"  # у закончившегося тариф переведён на бесплатный
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


async def test_privacy_command(maker):
    from app.bot.setup import privacy

    m = MagicMock()
    m.answer = AsyncMock()
    await privacy(m)
    assert "telegra.ph" in m.answer.call_args.args[0]


async def test_receipts_reminder_once_a_month(maker, monkeypatch):
    from datetime import datetime

    from app.models import Payment
    from app.services import quiet, receipts

    async with maker() as s:
        s.add(Payment(user_id=1, days=30, amount=49000, currency="RUB", charge_id="a", created_at=datetime(2026, 9, 12)))
        s.add(Payment(user_id=1, days=30, amount=99000, currency="RUB", charge_id="b", created_at=datetime(2026, 9, 20)))
        s.add(Payment(user_id=1, days=30, amount=49000, currency="RUB", charge_id="c", created_at=datetime(2026, 9, 25), refunded=True))
        s.add(Payment(user_id=1, days=30, amount=49000, currency="RUB", charge_id="d", created_at=datetime(2026, 10, 2)))  # этот месяц
        await s.commit()
    monkeypatch.setattr(quiet, "local_now", lambda: datetime(2026, 10, 3, 12))
    async with maker() as s:
        assert await receipts.due_message(s) is None  # до 5-го числа рано
    monkeypatch.setattr(quiet, "local_now", lambda: datetime(2026, 10, 5, 12))
    async with maker() as s:
        text = await receipts.due_message(s)
    assert "оплат 2 на 1480 ₽" in text and "09.2026" in text  # возвращённый платёж не считается
    async with maker() as s:
        assert await receipts.due_message(s) is None  # повторно в этом месяце не напоминаем


async def test_weekly_backup_is_sent_to_admin(maker, tgbot, tmp_path, monkeypatch):
    from app.services import backup

    folder = tmp_path / "backups"
    folder.mkdir()
    (folder / "crm-2026-10-13.db").write_bytes(b"x")
    docs = []

    async def send_document(uid, file, caption=""):
        docs.append((uid, caption))

    tgbot.send_document = send_document
    assert await backup.send_latest(tgbot, [ADMIN_ID], folder) == 1
    assert docs[0][0] == ADMIN_ID and "копия базы" in docs[0][1]
    assert await backup.send_latest(tgbot, [ADMIN_ID], tmp_path / "нет") == 0  # копий нет — ничего не шлём


def test_main_background_loops_defined():
    """Все фоновые задачи, запускаемые в main(), существуют (иначе бот падает на старте с NameError)."""
    import ast
    import inspect

    from app import main as m

    src = inspect.getsource(m.main)
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id.endswith("_loop"):
            assert hasattr(m, node.func.id), node.func.id


async def test_sqlite_uses_wal_and_busy_timeout(tmp_path):
    """Файловая база SQLite открывается в режиме WAL с ожиданием блокировки (защита от «database is locked»)."""
    from sqlalchemy import text

    from app.db import make_engine
    from app.models import Base

    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with engine.connect() as conn:
            assert (await conn.execute(text("PRAGMA journal_mode"))).scalar() == "wal"
            assert (await conn.execute(text("PRAGMA busy_timeout"))).scalar() == 15000
            assert (await conn.execute(text("PRAGMA synchronous"))).scalar() == 1  # NORMAL
    finally:
        await engine.dispose()


async def test_concurrent_writers_do_not_lock(tmp_path):
    """Много одновременных записей и чтений не падают с «database is locked»."""
    import asyncio

    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.db import make_engine
    from app.models import Base, Setting

    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path}/c.db")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        maker = async_sessionmaker(engine, expire_on_commit=False)

        async def write(i):
            async with maker() as s:
                s.add(Setting(key=f"k{i}", value=str(i)))
                await s.commit()

        async def read():
            async with maker() as s:
                await s.scalars(select(Setting))

        await asyncio.gather(*[write(i) for i in range(40)], *[read() for _ in range(40)])
        async with maker() as s:
            assert len((await s.scalars(select(Setting))).all()) == 40
    finally:
        await engine.dispose()


async def test_winback_message_once_three_days_after_end(maker, tgbot):
    async with maker() as s:
        await settings.put(s, "price_30", "490")
    await make_user(maker, 91, plan="full", plan_until=now() - timedelta(hours=1))
    await expiry_once()
    assert any("закончился" in t for t in tgbot.texts(91))
    async with maker() as s:
        u = await s.get(User, 91)
        assert u.plan == "free" and u.winback_at is not None
        u.winback_at = now() - timedelta(minutes=1)  # прошло 3 дня
        await s.commit()
    await expiry_once()
    assert any("Вернуть полный тариф: 30 дн. — 490" in t for t in tgbot.texts(91))
    before = len(tgbot.texts(91))
    await expiry_once()
    assert len(tgbot.texts(91)) == before  # второй раз не пишем
    async with maker() as s:
        assert (await s.get(User, 91)).winback_at is None


async def test_cleanup_keeps_unfinished_account_with_payment_or_ad_order(maker):
    from app.models import AdOrder, Payment
    from app.services import cleanup

    async with maker() as s:
        old = now() - timedelta(days=30)
        s.add_all([User(tg_id=93, onboarded=False, created_at=old), User(tg_id=94, onboarded=False, created_at=old),
                   User(tg_id=95, onboarded=False, created_at=old)])
        s.add(Payment(user_id=93, days=30, amount=490, currency="RUB", charge_id="t1"))
        s.add(AdOrder(user_id=94, text="реклама", group_ids="", price=0, status="draft"))
        await s.commit()
    async with maker() as s:
        assert [u.tg_id for u in await cleanup.empty_users(s, 7)] == [95]  # с оплатой и заказом рекламы — остаются
