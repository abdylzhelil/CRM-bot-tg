"""Regression checks for SC-01…06: recovery, limits and source protection."""
import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from telethon.errors import FloodWaitError

from app.models import FeedChat, Group, ScoutAccount, SearchQuery, UserSearch, now
from app.scout import search, joiner, manager as manager_mod


async def test_local_manual_queries_run_in_their_own_tier(maker):
    async with maker() as s:
        s.add_all([SearchQuery(text='ремонт квартир Каспийск'),
                   SearchQuery(text='ремонт Грозный'), SearchQuery(text='ремонт Москва')])
        await s.commit()
        tiers=await search.tier_queries(s)
        assert 'ремонт квартир Каспийск' in tiers[0]
        assert 'ремонт Грозный' in tiers[1]
        assert 'ремонт Москва' in tiers[2]
        batch,stage,_=await search.next_batch(s)
        assert stage==0 and 'ремонт квартир Каспийск' in batch


async def test_only_enabled_feed_covers_group_and_join_checks_it(maker):
    async with maker() as s:
        s.add(FeedChat(tg_id=-98701,title='synthetic',enabled=False))
        await s.commit()
        assert not await search.is_covered(s,-98701)
        (await s.get(FeedChat,-98701)).enabled=True
        await s.commit()
        assert await search.is_covered(s,-98701)


async def test_disabled_feed_allows_approved_group_to_join(maker,monkeypatch):
    from app.scout import folders
    async with maker() as s:
        s.add(ScoutAccount(id=97,name='join',api_id=1,api_hash='test'))
        s.add_all([Group(tg_id=-98708,username='synthetic',title='Каспийск',city='Каспийск',
                         region='Дагестан',status='approved'),
                   FeedChat(tg_id=-98708,title='Каспийск',enabled=False)])
        await s.commit()
    client=AsyncMock();client.is_connected=lambda:True
    monkeypatch.setattr(joiner,'_quality',AsyncMock(return_value=('',{})))
    monkeypatch.setattr(folders,'mute',AsyncMock())
    monkeypatch.setattr(folders,'sync_folders',AsyncMock())
    assert await joiner.join_one(client,97)
    async with maker() as s:
        assert await s.scalar(select(Group.status).where(Group.tg_id==-98708))=='joined'


@pytest.mark.parametrize('protection',[{'catalog':True},{'submitted_by':70},{'wanted_by':70},{'ads_allowed':True}])
async def test_rotation_keeps_every_protected_source(maker,monkeypatch,protection):
    async with maker() as s:
        s.add(ScoutAccount(id=99,name='synthetic',api_id=1,api_hash='test'))
        s.add_all([Group(tg_id=-98702,title='protected',account_id=99,status='joined',
                         joined_at=now()-timedelta(days=60),**protection),
                   Group(tg_id=-98703,title='ordinary',account_id=99,status='joined',
                         joined_at=now()-timedelta(days=60))])
        await s.commit()
    class Client:
        def __init__(self):self.left=[]
        async def get_entity(self,gid):return gid
        async def __call__(self,request):self.left.append(request.channel)
    client=Client()
    monkeypatch.setattr(joiner.asyncio,'sleep',AsyncMock())
    assert await joiner.rotate(client,99,limit=1)==1
    async with maker() as s:
        groups={g.tg_id:g.status for g in (await s.scalars(select(Group))).all()}
    assert groups[-98702]=='joined' and groups[-98703]=='left'


async def test_initial_failure_is_disconnected_and_recovered(maker,monkeypatch,tgbot):
    async with maker() as s:
        acc=ScoutAccount(id=98,name='retry',api_id=1,api_hash='test',session='synthetic')
        s.add(acc);await s.commit()
    failed=SimpleNamespace(connect=AsyncMock(side_effect=ConnectionError()),disconnect=AsyncMock())
    good=SimpleNamespace(connect=AsyncMock(),is_user_authorized=AsyncMock(return_value=True),
                         get_me=AsyncMock(return_value=SimpleNamespace(id=777)),
                         disconnect=AsyncMock(),add_event_handler=lambda *a:None)
    clients=iter([failed,good])
    monkeypatch.setattr(manager_mod,'make_client',lambda acc:next(clients))
    monkeypatch.setattr(manager_mod.notify,'alert',AsyncMock())
    m=manager_mod.ScoutManager()
    monkeypatch.setattr(m,'_tidy',AsyncMock())
    await m.attach(acc)
    assert not m.clients
    failed.disconnect.assert_awaited_once()
    await asyncio.gather(m.recover_missing(),m.recover_missing())
    assert m.clients[98] is good
    good.connect.assert_awaited_once()
    async with maker() as s:
        (await s.get(ScoutAccount,98)).enabled=False
        await s.commit()
    await m.recover_missing()
    assert not m.clients
    good.disconnect.assert_awaited_once()


async def test_recovery_skips_revoked_and_limited_accounts(maker,monkeypatch):
    async with maker() as s:
        s.add_all([ScoutAccount(name=status,api_id=1,api_hash='test',session='synthetic',status=status)
                   for status in ('dead','limited')])
        await s.commit()
    m=manager_mod.ScoutManager()
    attach=AsyncMock()
    monkeypatch.setattr(m,'_attach',attach)
    await m.start()
    await m.recover_missing()
    attach.assert_not_awaited()


async def test_posting_limit_does_not_disconnect_existing_reader(maker):
    async with maker() as s:
        acc=ScoutAccount(name='reader',api_id=1,api_hash='test',session='synthetic',status='limited')
        s.add(acc);await s.commit()
    m=manager_mod.ScoutManager()
    reader=SimpleNamespace(disconnect=AsyncMock())
    m.clients[acc.id]=reader
    await m.recover_missing()
    assert m.clients[acc.id] is reader
    reader.disconnect.assert_not_awaited()


async def test_floodwait_persists_cooldown_and_does_not_sleep(maker,monkeypatch):
    class Client:
        _crm_account_id=99
        async def __call__(self,request):raise FloodWaitError(request=request,capture=300)
    client=Client()
    sleep=AsyncMock()
    monkeypatch.setattr(search.asyncio,'sleep',sleep)
    with pytest.raises(search.SearchDeferred):
        await search._run_query(client,'Махачкала',{})
    sleep.assert_not_awaited()
    # A replacement client after restart must respect the saved deadline too.
    with pytest.raises(search.SearchDeferred):
        await search._check_deferred(Client())


async def test_sampling_does_not_hide_floodwait():
    class Client:
        async def iter_messages(self,*args,**kwargs):
            raise FloodWaitError(request=None,capture=300)
            yield  # async iterator, no messages returned
    with pytest.raises(FloodWaitError):await search._sample(Client(),object())


async def test_deferred_general_query_returns_unfinished_offset(maker,monkeypatch,tgbot):
    from app.scout import client as client_mod
    from app.services import queryideas,settings
    monkeypatch.setattr(client_mod,'ensure_connected',AsyncMock(return_value=True))
    monkeypatch.setattr(queryideas,'refresh',AsyncMock(return_value=0))
    monkeypatch.setattr(search.asyncio,'sleep',AsyncMock())
    run=AsyncMock(side_effect=[dict(found=0,known=0,small=0,junked=0,errors=0),search.SearchDeferred(300)])
    monkeypatch.setattr(search,'_run_query',run)
    with pytest.raises(search.SearchDeferred):await search.discover(object())
    async with maker() as s:
        assert await settings.raw(s,search.HOME_KEY)=='1'
        assert await search.stage(s)==0
    assert 'Telegram' in await search.last_search()


async def test_deferred_user_query_is_still_due(maker,monkeypatch):
    from app.scout import client as client_mod
    from tests.conftest import make_user
    await make_user(maker,77)
    async with maker() as s:
        s.add(UserSearch(user_id=77,text='Каспийск ремонт'))
        await s.commit()
    monkeypatch.setattr(client_mod,'ensure_connected',AsyncMock(return_value=True))
    monkeypatch.setattr(search,'_run_query',AsyncMock(side_effect=search.SearchDeferred(60)))
    with pytest.raises(search.SearchDeferred):await search.user_pass(object())
    async with maker() as s:
        assert (await s.scalar(select(UserSearch))).last_run_at is None


async def test_duplicate_search_and_user_pass_do_not_overlap(monkeypatch):
    entered=asyncio.Event();release=asyncio.Event()
    async def operation(client,report=False):
        entered.set();await release.wait();return 3
    run=AsyncMock(side_effect=operation)
    monkeypatch.setattr(search,'_discover',run)
    notify=AsyncMock();monkeypatch.setattr(search.notify,'_send',notify)
    client=object();first=asyncio.create_task(search.discover(client))
    try:
        await entered.wait()
        assert await search.discover(client,report=True)==0
        assert await search.user_pass(client)==0
        assert run.await_count==1
        assert 'уже идёт' in notify.call_args.args[0]
    finally:release.set()
    assert await first==3
    assert not search._search_lock.locked()


@pytest.mark.parametrize('mode,expected',[('auto','approved'),('semi','candidate')])
@pytest.mark.parametrize('old_status',['covered','joined'])
async def test_covered_group_is_rechecked_after_feed_disabled(maker,monkeypatch,mode,expected,old_status):
    from app.services import settings
    async with maker() as s:
        g=Group(tg_id=-98709,title='old title',status=old_status,wanted_by=77,wanted_search=7)
        s.add_all([g,FeedChat(tg_id=-98709,title='local',enabled=False)])
        await s.commit();gid=g.id
        assert not await search.is_covered(s,-98709)
        await settings.put(s,'mode',mode)
        options=await search._options(s)
    class Client:
        async def __call__(self,request):
            if type(request).__name__=='SearchRequest':
                return SimpleNamespace(chats=[SimpleNamespace(megagroup=True,id=-98709,title='Каспийск объявления',username='synthetic')])
            return SimpleNamespace(full_chat=SimpleNamespace(participants_count=150,about='Каспийск'))
    monkeypatch.setattr(search,'Channel',SimpleNamespace)
    monkeypatch.setattr(search.utils,'get_peer_id',lambda c:c.id)
    monkeypatch.setattr(search,'_sample',AsyncMock(return_value=(5,[])))
    monkeypatch.setattr(search.asyncio,'sleep',AsyncMock())
    assert (await search._run_query(Client(),'Каспийск',options))['found']==1
    async with maker() as s:
        rows=(await s.scalars(select(Group).where(Group.tg_id==-98709))).all()
        assert len(rows)==1 and rows[0].id==gid and rows[0].status==expected
        assert rows[0].wanted_by==77 and rows[0].wanted_search==7


async def test_disabled_feed_does_not_duplicate_active_crm_bot(maker):
    async with maker() as s:
        s.add_all([Group(tg_id=-98710,title='own bot',status='joined',bot_member=True),
                   FeedChat(tg_id=-98710,title='own bot',enabled=False)])
        await s.commit()
        assert await search.is_covered(s,-98710)


async def test_interrupted_wrapped_batch_does_not_complete_region(maker,monkeypatch,tgbot):
    from app.scout import client as client_mod
    from app.services import queryideas,settings
    async with maker() as s:
        tiers=await search.tier_queries(s)
        await settings.put(s,search.HOME_KEY,str(len(tiers[0])-5))
        await s.commit()
    monkeypatch.setattr(client_mod,'ensure_connected',AsyncMock(return_value=True))
    monkeypatch.setattr(queryideas,'refresh',AsyncMock(return_value=0))
    monkeypatch.setattr(search,'_run_query',AsyncMock(side_effect=search.SearchDeferred(300)))
    with pytest.raises(search.SearchDeferred):await search.discover(object())
    async with maker() as s:
        assert await settings.raw(s,search.HOME_KEY)==str(len(tiers[0])-5)
        assert await settings.raw(s,search.STAGE_WRAP_KEY)=='0'
        assert await search.stage(s)==0


async def test_cancellation_restores_reserved_queries_and_releases_search(maker,monkeypatch,tgbot):
    from app.scout import client as client_mod
    from app.services import queryideas,settings
    monkeypatch.setattr(client_mod,'ensure_connected',AsyncMock(return_value=True))
    monkeypatch.setattr(queryideas,'refresh',AsyncMock(return_value=0))
    monkeypatch.setattr(search,'_run_query',AsyncMock(side_effect=asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):await search.discover(object())
    async with maker() as s:
        assert await settings.raw(s,search.HOME_KEY)=='0'
    assert not search.is_searching()
