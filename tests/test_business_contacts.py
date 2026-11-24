import csv
import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from sqlalchemy import select
from app.models import Group, Setting
from app.services import business_contacts as svc


def test_only_published_business_context_is_used():
    page='<meta property="og:description" content="По рекламе @Business05&#10;Участники @private_person&#10;Подписка @somechannel">'
    assert list(svc.extract(page)) == ['business05']
    assert svc.extract('<p>Администратор @hidden_person</p>') == {}


async def test_sources_are_dagestan_only(maker):
    async with maker() as s:
        s.add_all([Group(tg_id=-11,title='Ремонт Махачкала',username='local_one'),
                   Group(tg_id=-12,title='Дагестан',username='moscow_one',region='Москва'),
                   Group(tg_id=-13,title='Махачкала',username='private+bad',region='Дагестан'),
                   Group(tg_id=-14,title='Москва',username='other_region',region='Москва')])
        await s.commit()
        assert [g.username for g in await svc.sources(s)] == ['local_one']


async def test_merge_deduplicates_and_preserves_refusal(maker,monkeypatch):
    monkeypatch.setattr(svc,'prior_status',lambda _: 'new')
    async with maker() as s:
        g=Group(tg_id=-20,title='Дагестан',username='business_group')
        s.add(g);await s.commit()
        assert await svc.merge(s,g,{'contact05':'По рекламе @contact05'}) == 1
        data=await svc.load(s);data['contacts']['contact05']['status']='no';await svc.save(s,data)
        assert await svc.merge(s,g,{'contact05':'По вопросам @contact05'}) == 0
        row=(await svc.load(s))['contacts']['contact05']
        assert row['status']=='no' and len(row['sources'])==1
        assert row['sources'][0]['evidence']=='По вопросам @contact05'


async def test_search_persists_results_and_throttles(maker,monkeypatch):
    async with maker() as s:
        s.add(Group(tg_id=-21,title='Дагестан',username='business_group',region='Дагестан'))
        await s.commit()
    monkeypatch.setattr(svc,'fetch',AsyncMock(return_value='<meta property="og:description" content="По рекламе @contact05">'))
    result=await svc.search()
    assert result['added']==1 and result['checked']==1
    with pytest.raises(svc.ContactError,match='10 минут'):await svc.search()
    async with maker() as s:assert 'contact05' in (await svc.load(s))['contacts']


async def test_corrupt_storage_is_not_overwritten(maker):
    async with maker() as s:
        s.add(Setting(key=svc.KEY,value='{broken'))
        await s.commit()
        with pytest.raises(svc.ContactError):await svc.load(s)
        assert (await s.get(Setting,svc.KEY)).value=='{broken'


def test_export_has_source_and_prevents_spreadsheet_formulas():
    data={'contacts':{'contact05':{'handle':'contact05','status':'no','sources':[{'url':'https://t.me/source','evidence':'=DANGER()'}]}}}
    rows=list(csv.reader(io.StringIO(svc.export(data).decode('utf-8-sig'))))
    assert rows[1][0]=="'@contact05" and rows[1][4]=="'=DANGER()"
    assert rows[1][-1]=='Не проверена'


async def test_owner_only_router_and_menu(maker):
    from app.bot import setup
    from app.bot.handlers import business_contacts as h
    panel=lambda owner:[b.callback_data for row in setup.admin_panel(owner=owner).inline_keyboard for b in row]
    assert 'dc:menu' in panel(True) and 'dc:menu' not in panel(False)
    for uid,kind,allowed in [(1,'private',True),(2,'private',False),(1,'group',False)]:
        cb=SimpleNamespace(from_user=SimpleNamespace(id=uid),message=SimpleNamespace(chat=SimpleNamespace(type=kind)))
        assert (await h.router.callback_query.check_root_filters(cb))[0]==allowed
