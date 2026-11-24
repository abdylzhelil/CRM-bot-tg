from types import SimpleNamespace
from unittest.mock import AsyncMock
from sqlalchemy import select
from app.models import Group,User,Payment,CreditTx
from app.services import partner_report,boards
from tests.conftest import make_user


async def test_partner_sees_only_recorded_commission_payments(maker):
    await make_user(maker,71,credits=50,credits_hold=20)
    await make_user(maker,72)
    async with maker() as s:
        group=Group(tg_id=-901,title='partner',board_admin=71)
        s.add(group);await s.flush()
        (await s.get(User,72)).source=boards.source_tag(group.id)
        s.add_all([
            Payment(user_id=72,days=30,amount=49000,charge_id='mine',currency='RUB'),
            Payment(user_id=72,days=30,amount=99000,charge_id='other',currency='RUB'),
            Payment(user_id=72,days=0,amount=10000,charge_id='advert',currency='RUB'),
            CreditTx(user_id=71,amount=98,kind='board',ref='pay-mine'),
            CreditTx(user_id=71,amount=0,kind='board',ref='pay-mine'),
            CreditTx(user_id=999,amount=198,kind='board',ref='pay-other'),
            CreditTx(user_id=71,amount=20,kind='admin',ref='pay-advert'),
        ])
        await s.commit()
        result=await partner_report.summary(s,71)
        assert result['groups']==1 and result['visitors']==1
        assert result['payments']==1 and result['gross_kop']==49000
        assert result['commission_credits']==98
        assert result['available_credits']==50 and result['held_credits']==20
        assert not s.dirty


async def test_refunds_and_partner_without_groups(maker):
    await make_user(maker,73)
    async with maker() as s:
        s.add_all([
            Payment(user_id=70,days=30,amount=10000,currency='RUB',charge_id='partial',refunded_amount=2000),
            Payment(user_id=70,days=30,amount=20000,currency='RUB',charge_id='full',refunded=True),
            CreditTx(user_id=73,amount=20,kind='board',ref='pay-partial'),
            CreditTx(user_id=73,amount=40,kind='board',ref='pay-full'),
        ]);await s.commit()
        result=await partner_report.summary(s,73)
        assert result['groups']==0 and result['visitors']==0
        assert result['gross_kop']==30000 and result['refunds_kop']==22000 and result['net_kop']==8000
        assert (await partner_report.summary(s,999))['gross_kop']==0


async def test_partner_callback_never_accepts_another_user_id(maker,monkeypatch):
    from app.bot.handlers import credits
    await make_user(maker,74)
    reply=AsyncMock();monkeypatch.setattr(credits,'reply',reply)
    await credits.partner_report(SimpleNamespace(data='cr:partner'),SimpleNamespace(tg_id=74))
    text=reply.call_args.args[1]
    assert 'вывода деньгами нет' in text
    assert 'Оплат подписок с записанной вам комиссией: 0' in text
