"""Owner's public business-contact list. Never enumerates members or sends messages."""
import asyncio
import csv
import io
import json
import re
from pathlib import Path
from datetime import timedelta
from html.parser import HTMLParser

import aiohttp
from sqlalchemy import select

from app.db import Session
from app.models import Group, now
from app.services import geo, settings

KEY = 'dagestan_business_contacts_v1'
STATUSES = {'new': 'Новые', 'sent': 'Написал', 'replied': 'Ответили', 'no': 'Отказ', 'skip': 'Отложены'}
HANDLE = re.compile(r'^[A-Za-z][A-Za-z0-9_]{4,31}$')
MENTION = re.compile(r'(?<![\w@])@([A-Za-z][A-Za-z0-9_]{4,31})\b')
BUSINESS = re.compile(r'по\s+(?:всем\s+)?вопросам|сотруднич|по\s+рекламе|реклама\s*[-—:]|администратор|для\s+заказ|для\s+записи|менеджер|связаться|контакты\s*:', re.I)
_lock = asyncio.Lock()
_scan = asyncio.Lock()
_stop = asyncio.Event()


class ContactError(ValueError):
    pass


class Meta(HTMLParser):
    def __init__(self):
        super().__init__(); self.description = ''

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == 'meta' and a.get('property') == 'og:description':
            self.description = a.get('content', '')[:12000]


def extract(page):
    parser = Meta(); parser.feed(page)
    found = {}
    for line in parser.description.splitlines():
        # Context is the published description, never participant/admin API data.
        for match in MENTION.finditer(line):
            context = line[max(0, match.start()-110):match.end()+70]
            if BUSINESS.search(context):
                found.setdefault(match[1].lower(), context.strip()[:240])
    return found


async def fetch(client, username):
    if not HANDLE.fullmatch(username):
        raise ContactError('Некорректный публичный адрес')
    async with client.get('https://t.me/'+username, allow_redirects=False) as r:
        if r.status != 200:
            raise ContactError('Источник не ответил')
        raw = await r.content.read(262145)
        if len(raw) > 262144:
            raise ContactError('Источник слишком большой')
        return raw.decode('utf-8', errors='replace')


async def load(s):
    raw = await settings.raw(s, KEY)
    if not raw:
        return {'contacts': {}, 'last_scan': '', 'report': {}}
    try:
        data = json.loads(raw)
        if not isinstance(data.get('contacts'), dict):
            raise ValueError
        return data
    except (ValueError, TypeError):
        raise ContactError('Список повреждён — поиск остановлен, данные не перезаписаны') from None


async def save(s, data):
    await settings.put(s, KEY, json.dumps(data, ensure_ascii=False))
    await s.commit()


async def sources(s):
    groups = (await s.scalars(select(Group).where(Group.username != '', Group.status.not_in(('rejected','draft','left'))))).all()
    return [g for g in groups if HANDLE.fullmatch(g.username) and
            (g.region == 'Дагестан' or (not g.region and geo.title_place(g.title)[1] == 'Дагестан'))][:100]


def prior_status(handle):
    """Respect the owner's earlier one-off inquiries, without exposing private dialogs."""
    for campaign in ('partner-credits-2026-11-24-v1', 'partner-terms-2026-11-15-v1'):
        path = Path('/srv/data/partner_inquiries') / campaign / 'state.json'
        if not path.exists():
            continue
        try:
            actions = json.loads(path.read_text()).get('actions', {})
            for key, row in actions.items():
                if key.split(':')[0].lower() == handle.lower():
                    return 'sent' if row.get('status') == 'sent' else 'skip'
        except (OSError, ValueError, TypeError, AttributeError):
            return 'skip'  # uncertainty must never re-open an old contact
    return 'new'


async def merge(s, group, found):
    data = await load(s)
    added = 0
    for handle, evidence in found.items():
        if handle not in data['contacts']:
            if len(data['contacts']) >= 500:
                break
            data['contacts'][handle] = {'handle': handle, 'status': prior_status(handle), 'sources': []}
            added += 1
        row = data['contacts'][handle]
        src = {'url': 'https://t.me/'+group.username, 'title': group.title[:120],
               'evidence': evidence, 'checked_at': now().isoformat(),
               'source_last_lead_at': group.last_lead_at.isoformat() if group.last_lead_at else ''}
        row['sources'] = [x for x in row['sources'] if x['url'] != src['url']][-4:]+[src]
        row['checked_at'] = src['checked_at']
        # Rechecking never clears a refusal or an owner's delivery marker.
        if group.offer_status == 'no':
            row['status'] = 'no'
        elif group.offer_status == 'skip' and row['status'] == 'new':
            row['status'] = 'skip'
    await save(s, data)
    return added


async def mark(handle, status):
    if status not in STATUSES:
        raise ContactError('Неизвестный статус')
    async with _lock:
        async with Session() as s:
            data = await load(s)
            if handle not in data['contacts']:
                raise ContactError('Контакт не найден')
            data['contacts'][handle]['status'] = status
            data['contacts'][handle]['status_at'] = now().isoformat()
            await save(s, data)


def cancel():
    _stop.set()


async def search():
    if _scan.locked():
        raise ContactError('Поиск уже идёт. Можно остановить его кнопкой.')
    async with _scan:
        _stop.clear()
        async with Session() as s:
            data = await load(s)
            if data.get('last_scan') and now().isoformat() < data['last_scan']:
                raise ContactError('Проверьте время сервера')
            from datetime import datetime
            if data.get('last_scan') and now()-datetime.fromisoformat(data['last_scan']) < timedelta(minutes=10):
                raise ContactError('Повторный поиск доступен через 10 минут; список уже сохранён.')
            groups = await sources(s)
        report = {'sources': len(groups), 'checked': 0, 'errors': 0, 'added': 0, 'stopped': False}
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as client:
            for group in groups:
                if _stop.is_set():
                    report['stopped'] = True; break
                try:
                    found = extract(await fetch(client, group.username))
                    async with _lock:
                        async with Session() as s:
                            report['added'] += await merge(s, group, found)
                    report['checked'] += 1
                except (aiohttp.ClientError, asyncio.TimeoutError, ContactError):
                    report['errors'] += 1
                await asyncio.sleep(.2)
        async with _lock:
            async with Session() as s:
                data = await load(s)
                data.update(last_scan=now().isoformat(), report=report)
                await save(s, data)
        return report


def export(data):
    out = io.StringIO(); writer = csv.writer(out)
    writer.writerow(['Контакт','Статус','Проверено','Источник','Контекст публикации','Активность контакта'])
    def safe(value):
        value = str(value)
        return "'"+value if value.startswith(('=', '+', '-', '@', '\t', '\r')) else value
    for row in data['contacts'].values():
        for src in row['sources']:
            writer.writerow([safe('@'+row['handle']), STATUSES[row['status']], row.get('checked_at',''),
                             src['url'], safe(src['evidence']), 'Не проверена'])
    return ('\ufeff'+out.getvalue()).encode('utf-8')
