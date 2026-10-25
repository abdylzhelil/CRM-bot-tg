"""💰 Кредиты: комиссия, обмен на дни 💎 и коды бота модерации, возврат оплаты, ручное начисление."""
import pytest

from app.models import CreditTx, Group, ModCode, Payment, User, now
from app.services import credits, payments, settings


async def _user(s, tg_id=5001, amount=0):
    u = User(tg_id=tg_id, name=f"u{tg_id}", onboarded=True, credits=amount)
    s.add(u)
    await s.commit()
    return u


async def test_add_and_no_negative(maker):
    async with maker() as s:
        u = await _user(s)
        await credits.add(s, u, 100, "admin", "бонус")
        assert await credits.add(s, u, 50, "board", ref="pay-x") is not None
        assert await credits.add(s, u, 50, "board", ref="pay-x") is None  # повтор по ref
        with pytest.raises(credits.CreditError):
            await credits.add(s, u, -500, "days")
        await s.commit()
        assert u.credits == 150
        assert [t.amount for t in await credits.history(s, 5001)] == [50, 100]
        assert credits.commission(129000, 20) == 258


async def test_buy_days(maker):
    async with maker() as s:
        await settings.put(s, "price_30", "490")
        u = await _user(s, amount=500)
        assert await credits.day_offers(s) == [(30, 490)]
        assert await credits.buy_days(s, u, 30) == 490
        await s.commit()
        assert u.credits == 10 and u.plan == "full"
        with pytest.raises(credits.CreditError):
            await credits.buy_days(s, u, 30)  # не хватает
        with pytest.raises(credits.CreditError):
            await credits.buy_days(s, u, 90)  # не продаётся


async def test_mod_codes_stock_and_waiting(maker):
    async with maker() as s:
        u = await _user(s, amount=1000)
        stocked, filled = await credits.add_codes(s, "monthly", "abcd-efgh-jklm\nABCD-EFGH-JKLM, QWER-TYUI-OPAS")
        assert stocked == 2 and not filled and await credits.in_stock(s, "monthly") == 2
        mc = await credits.buy_mod(s, u, "monthly")
        assert mc.code == "ABCD-EFGH-JKLM" and u.credits == 750
        mc2 = await credits.buy_mod(s, u, "monthly")
        mc3 = await credits.buy_mod(s, u, "monthly")  # запас кончился — заявка ждёт
        assert mc2.code and mc3.code == "" and u.credits == 250
        await s.commit()
        assert [w.id for w in await credits.waiting_of(s)] == [mc3.id]
        stocked, filled = await credits.add_codes(s, "monthly", "ZZZZ-XXXX-CCCC ABCD-EFGH-JKLM")  # известный код не дублируем
        assert stocked == 0 and [f.id for f in filled] == [mc3.id] and mc3.code == "ZZZZ-XXXX-CCCC"
        with pytest.raises(credits.CreditError):
            await credits.cancel_waiting(s, mc3)  # уже выдан


async def test_cancel_waiting_returns_credits(maker):
    async with maker() as s:
        u = await _user(s, amount=300)
        mc = await credits.buy_mod(s, u, "monthly")
        assert await credits.cancel_waiting(s, mc) == 250
        await s.commit()
        assert u.credits == 300 and await credits.waiting_of(s) == []


async def _group_sale(maker, admin_exists=True, charge="ch-9"):
    async with maker() as s:
        g = Group(tg_id=-1, title="Группа", status="joined", board_admin=777)
        s.add(g)
        await s.flush()
        if admin_exists:
            s.add(User(tg_id=777, name="Админ", onboarded=True))
        s.add(User(tg_id=888, name="Покупатель", onboarded=True, source=f"g{g.id}"))
        await s.commit()
    async with maker() as s:
        await payments.apply(s, 888, 30, 100000, "RUB", charge)


async def _age_holds(maker):
    from datetime import timedelta

    from sqlalchemy import select

    async with maker() as s:
        for tx in (await s.scalars(select(CreditTx).where(CreditTx.hold_until.is_not(None)))).all():
            tx.hold_until = now() - timedelta(minutes=1)
        await s.commit()


async def test_refund_while_frozen_cancels_commission(maker, tgbot):
    """Вернули оплату в срок возврата — комиссия ещё заморожена и просто отменяется (админ её не потратил)."""
    from sqlalchemy import select

    await _group_sale(maker)
    async with maker() as s:
        u = await s.get(User, 777)
        assert (u.credits, u.credits_hold) == (0, 200)
        p = await s.scalar(select(Payment).where(Payment.charge_id == "ch-9"))
        await payments.take_back(s, p)
    async with maker() as s:
        u = await s.get(User, 777)
        assert (u.credits, u.credits_hold) == (0, 0)
        assert await credits.reverse_payment(s, "ch-9") == 0  # второй раз не списываем
    await _age_holds(maker)
    async with maker() as s:
        assert await credits.release_due(s) == []  # отменённое не размораживается
        await s.commit()
        assert (await s.get(User, 777)).credits == 0


async def test_release_after_refund_window_then_refund_takes_rest(maker, tgbot):
    from sqlalchemy import select

    from app.bot.handlers import credits as h

    await _group_sale(maker)
    await _age_holds(maker)
    assert await h.release_once() == 1
    async with maker() as s:
        u = await s.get(User, 777)
        assert (u.credits, u.credits_hold) == (200, 0)
        await credits.add(s, u, -150, "days")  # потратил часть
        await s.commit()
        p = await s.scalar(select(Payment).where(Payment.charge_id == "ch-9"))
        await payments.take_back(s, p)
    async with maker() as s:
        assert (await s.get(User, 777)).credits == 0  # списали, сколько было


async def test_admin_not_in_bot_gets_credits_later(maker, tgbot):
    """Админ добавил бота в группу, но сам бота не открывал: кредиты ждут и зачисляются при первом входе."""
    from app.bot.handlers import credits as h
    from app.services import cleanup

    await _group_sale(maker, admin_exists=False)
    await _age_holds(maker)
    assert await h.release_once() == 0
    async with maker() as s:
        s.add(User(tg_id=777, name="Пришёл", onboarded=False))
        await s.commit()
        u = await s.get(User, 777)
    await h.claim_on_start(u)
    async with maker() as s:
        u = await s.get(User, 777)
        assert u.credits == 200
        assert u not in await cleanup.empty_users(s, -1)  # человек с кредитами — не «пустой аккаунт»


async def test_menu_and_admin_screens(maker, tgbot):
    from app.bot.handlers import credits as h

    async with maker() as s:
        u = await _user(s, amount=50)
        await credits.add(s, u, 10, "admin", "тест")
        await s.commit()
    from unittest.mock import AsyncMock, MagicMock

    m = MagicMock()
    m.answer = AsyncMock()
    await h.show(m, u)
    text = m.answer.call_args.args[0]
    assert "Мои кредиты: 60" in text and "от владельца" in text
    await h.admin_view(m)
    assert "Коды бота модерации" in m.answer.call_args.args[0]


async def test_erase_removes_credits(maker):
    from app.services import erase

    async with maker() as s:
        u = await _user(s, amount=300)
        await credits.buy_mod(s, u, "monthly")
        await credits.add(s, u, 5, "admin")
        await s.commit()
        assert await erase.erase_user(s, 5001)
        await s.commit()
    async with maker() as s:
        from sqlalchemy import func, select

        assert await s.scalar(select(func.count()).select_from(CreditTx)) == 0
        assert await s.scalar(select(func.count()).select_from(ModCode)) == 0


async def test_waiting_code_reminder_and_selfcheck(maker, tgbot):
    from datetime import timedelta

    from app.bot.handlers import credits as h
    from app.services import selfcheck

    async with maker() as s:
        u = await _user(s, amount=300)
        mc = await credits.buy_mod(s, u, "monthly")
        mc.sold_at = now() - timedelta(hours=13)
        await s.commit()
    assert await h.remind_waiting()
    assert not await h.remind_waiting()  # не чаще раза в 12 ч
    assert any("Ждут код" in t for _, t in tgbot.sent)
    async with maker() as s:
        rows = await selfcheck.run(s)
    assert any(name == "Коды бота модерации" and "12 ч" in note for _, name, note in rows)
