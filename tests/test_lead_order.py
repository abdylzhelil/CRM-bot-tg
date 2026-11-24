from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import pytest
from sqlalchemy import select
from app.models import Lead, Delivery, User
from app.services import lead_order
from app.bot.handlers import leads
from tests.conftest import make_user
from tests.test_webapp import client, tma


async def dataset(maker):
    await make_user(maker,71)
    await make_user(maker,72)
    ids=[]
    async with maker() as s:
        # Delivery IDs intentionally contradict source timestamps; tied timestamps too.
        for i, day in enumerate([3,1,3,2]):
            lead=Lead(author_id=100+i,text=f'SORT-{i}',text_hash=f'sort-{i}',created_at=datetime(2026,11,day),
                      name='',username='',group_title='Test',city='Махачкала',region='Дагестан')
            s.add(lead);await s.flush()
            d=Delivery(user_id=71,lead_id=lead.id,status='new',sent=True)
            s.add(d);await s.flush();ids.append(d.id)
        foreign=Delivery(user_id=72,lead_id=lead.id,status='new',sent=True)
        s.add(foreign);await s.commit()
        return ids,foreign.id


async def test_private_preference_and_validation(maker):
    await make_user(maker,71)
    async with maker() as s:
        assert await lead_order.get(s,71)=='desc'
        await lead_order.put(s,71,'asc')
        assert await lead_order.get(s,71)=='asc' and await lead_order.get(s,72)=='desc'
        with pytest.raises(ValueError):await lead_order.put(s,71,'bad')


@pytest.mark.parametrize('order,expected',[('asc',[1,3,0,2]),('desc',[2,0,3,1])])
async def test_cursor_date_order_ties_and_changed_status(maker,order,expected):
    ids,foreign=await dataset(maker)
    async with maker() as s:
        q=select(Delivery.id).join(Lead,Lead.id==Delivery.lead_id).where(Delivery.user_id==71,Delivery.status=='new')
        rows=list((await s.scalars(q.order_by(*lead_order.columns(order)))).all())
        assert rows==[ids[i] for i in expected]
        cursor=await lead_order.cursor(s,71,rows[0])
        (await s.get(Delivery,rows[0])).status='rejected';await s.commit()
        rest=list((await s.scalars(q.where(lead_order.boundary(cursor,order)).order_by(*lead_order.columns(order)))).all())
        assert rest==rows[1:]
        assert await lead_order.cursor(s,71,foreign) is None


async def test_telegram_switch_navigation_and_filter_survive(maker,monkeypatch):
    ids,_=await dataset(maker)
    output=AsyncMock();monkeypatch.setattr(leads,'reply',output)
    cb=MagicMock();cb.answer=AsyncMock()
    user=SimpleNamespace(tg_id=71,is_admin=False,plan='full')
    cb.data='lo:set:new:0:0:asc'
    await leads.order_card(cb,user)
    text=output.call_args.args[1];markup=output.call_args.args[2]
    assert 'SORT-1' in text and '1 из 4' in text and 'Сначала старые' in text
    assert any(b.callback_data=='lo:set:new:0:0:desc' for row in markup.inline_keyboard for b in row)
    cb.message.reply_markup=markup
    assert any(b.callback_data.startswith('lo:') for row in leads._nav_of(cb) for b in row)
    async with maker() as s:
        (await s.get(Delivery,ids[1])).status='taken';await s.commit()
    await leads.show_one(cb,user,'new',0,ids[1],'n')
    assert 'SORT-3' in output.call_args.args[1] and '1 из 3' in output.call_args.args[1]
    await leads.show_one(cb,user,'new',0,ids[0],'p')
    assert 'SORT-3' in output.call_args.args[1]


async def test_api_order_pagination_private_and_pinned(client,maker,monkeypatch):
    from app.web import api
    ids,foreign=await dataset(maker)
    monkeypatch.setattr(api,'PAGE',2)
    assert (await client.post('/api/lead-order',json={'sort':'asc'})).status==401
    for bad in ('bad', None, [], {}):
        assert (await client.post('/api/lead-order',headers=tma(71),json={'sort':bad})).status==400
    assert (await client.post('/api/lead-order',headers=tma(71),json={'sort':'asc'})).status==200
    first=await (await client.get('/api/leads',headers=tma(71))).json()
    assert first['sort']=='asc' and [x['id'] for x in first['items']]==[ids[1],ids[3]]
    # Other UI changes saved preference; explicitly pinned page stays in its order.
    await client.post('/api/lead-order',headers=tma(71),json={'sort':'desc'})
    rest=await (await client.get(f'/api/leads?before={ids[3]}&sort=asc',headers=tma(71))).json()
    assert [x['id'] for x in rest['items']]==[ids[0],ids[2]]
    assert (await client.get(f'/api/leads?before={foreign}',headers=tma(71))).status==400
    assert (await client.get('/api/leads?sort=bad',headers=tma(71))).status==400
    async with maker() as s:assert await lead_order.get(s,72)=='desc'
