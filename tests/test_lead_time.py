from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from app.services.lead_time import found_label, PUBLICATION_NOTE


def test_moscow_date_and_relative_age():
    lead=SimpleNamespace(created_at=datetime(2026,11,24,20,50))
    text=found_label(lead,datetime(2026,11,24,21,10))
    assert 'вчера, 24.11.2026 в 23:50 МСК' in text and '20 мин. назад' in text
    assert 'Опубликована' not in text


def test_aware_utc_missing_and_future():
    lead=SimpleNamespace(created_at=datetime(2026,11,24,12,tzinfo=timezone.utc))
    assert 'сегодня, 24.11.2026 в 15:00 МСК · 2 ч. назад' in found_label(lead,datetime(2026,11,24,14))
    assert 'не сохранено' in found_label(SimpleNamespace(created_at=None))
    assert 'время требует проверки' in found_label(lead,datetime(2026,11,24,11))


async def test_card_and_cabinet_share_honest_time_label(maker,monkeypatch):
    from app.models import Lead, Delivery
    from app.bot.notify import lead_text
    from app.web.api import lead_item
    lead=Lead(author_id=22,text='Нужен мастер',text_hash='date-check',created_at=datetime(2025,1,1),
              name='Test',username='',group_title='Test',city='',region='',suspicious=False,link='https://t.me/example/1')
    text=lead_text(lead,'Ремонт')
    assert '01.01.2025 в 03:00 МСК' in text and PUBLICATION_NOTE in text
    assert '🆕 Новый клиент' not in text
    async with maker() as s:
        s.add(lead);await s.flush()
        d=Delivery(user_id=1,lead_id=lead.id);s.add(d);await s.flush()
        item=await lead_item(s,d,lead)
        assert item['found_at_label']==found_label(lead)
        assert item['publication_note']==PUBLICATION_NOTE
