from datetime import timedelta
import pytest
from sqlalchemy import select
from app.models import AdOrder, CreditTx, Group, User, now
from app.services import adposts, boards, credits, payments, partner_report
from tests.conftest import make_user


async def sale(maker, partner=True):
    if partner:
        await make_user(maker, 81)
    await make_user(maker, 82)
    async with maker() as s:
        g = Group(tg_id=-8081, title='Дагестан', board_admin=81)
        s.add(g)
        await s.flush()
        (await s.get(User,82)).source = boards.source_tag(g.id)
        order = AdOrder(user_id=82, text='test advert', status='approved', price=1000)
        s.add(order)
        await s.commit()
        result = await adposts.mark_paid(s, order.id, 'partner-ad', 100000)
        assert result
        assert await adposts.mark_paid(s, order.id, 'partner-ad', 100000) is None
        return result[1].id


async def test_ad_payment_reward_and_private_report(maker):
    await sale(maker)
    async with maker() as s:
        assert (await s.get(User,81)).credits_hold == 200
        assert len((await s.scalars(select(CreditTx))).all()) == 1
        data = await partner_report.summary(s,81)
        assert data['ad_payments'] == 1 and data['subscription_payments'] == 0
        assert data['gross_kop'] == 100000
        assert (await partner_report.summary(s,82))['gross_kop'] == 0


@pytest.mark.parametrize('registered', [True, False])
async def test_partial_then_full_reversal_and_claim(maker, registered):
    await sale(maker,registered)
    async with maker() as s:
        await credits.reverse_payment(s,'yk:partner-ad',25000,100000)
        await credits.reverse_payment(s,'yk:partner-ad',25000,100000)
        await s.commit()
    if not registered:
        await make_user(maker,81)
        async with maker() as s:
            assert await credits.claim(s,await s.get(User,81)) == 150
            await s.commit()
    async with maker() as s:
        u = await s.get(User,81)
        assert (u.credits,u.credits_hold) == (0,150)
        await credits.reverse_payment(s,'yk:partner-ad')
        await credits.reverse_payment(s,'yk:partner-ad')
        await s.commit()
        assert (u.credits,u.credits_hold) == (0,0)
        tx = await s.scalar(select(CreditTx).where(CreditTx.kind=='board'))
        tx.hold_until = now()-timedelta(days=1)
        assert await credits.release_due(s) == []


async def test_partial_refund_releases_only_remaining_commission(maker):
    from app.models import Payment
    pid = await sale(maker)
    async with maker() as s:
        await payments.mark_manual_refund(s,await s.get(Payment,pid),25000)
        tx = await s.scalar(select(CreditTx).where(CreditTx.kind=='board'))
        tx.hold_until = now()-timedelta(days=1)
        assert await credits.release_due(s) == [(81,150)]
        await s.commit()
        u=await s.get(User,81)
        assert (u.credits,u.credits_hold)==(150,0)
        await payments.take_back(s,await s.get(Payment,pid))
        assert (u.credits,u.credits_hold)==(0,0)


@pytest.mark.parametrize("remaining", [0, 20])
async def test_released_spent_credits_do_not_go_negative(maker, remaining):
    await sale(maker)
    async with maker() as s:
        tx=await s.scalar(select(CreditTx).where(CreditTx.kind=='board'))
        tx.hold_until=now()-timedelta(days=1)
        await credits.release_due(s)
        u=await s.get(User,81)
        u.credits=remaining
        assert await credits.reverse_payment(s,'yk:partner-ad')==remaining
        u.credits=50
        assert await credits.reverse_payment(s,'yk:partner-ad')==0
        assert u.credits==50
