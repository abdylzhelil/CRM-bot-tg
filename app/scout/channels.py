"""Источник «открытые каналы»: читаем публичную веб-версию t.me/s/<канал> — без аккаунта и без вступления.

Только каналы с включённым веб-просмотром. Пост считаем заявкой, только если в нём указан контакт (@ник):
у постов канала нет автора-человека, а писать нужно тому, кто разместил объявление.
"""
import asyncio
import html
import logging
import random
import re
import zlib

import aiohttp
from sqlalchemy import select

from app.db import Session
from app.feed import links
from app.models import Group
from app.services import intake, settings
from app.services.antispam import AuthorInfo
from app.services.pipeline import Incoming

log = logging.getLogger(__name__)

_POST = re.compile(r'data-post="([A-Za-z0-9_]+)/(\d+)"')
_TEXT = re.compile(r'tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', re.S)
_CONTACT = re.compile(r"(?<![\w/])@([A-Za-z][A-Za-z0-9_]{4,31})\b|t\.me/([A-Za-z][A-Za-z0-9_]{4,31})\b")
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; CRMBot/1.0)"}


def parse_posts(page: str) -> list[tuple[int, str]]:
    """[(id поста, текст)] в порядке страницы (от старых к новым)."""
    out = []
    for chunk in page.split("tgme_widget_message_wrap")[1:]:
        post = _POST.search(chunk)
        if not post:
            continue
        m = _TEXT.search(chunk)
        text = ""
        if m:
            text = re.sub(r"<br\s*/?>", "\n", m.group(1))
            text = html.unescape(re.sub(r"<[^>]+>", "", text)).strip()
        out.append((int(post.group(2)), text))
    return out


async def fetch_posts(username: str) -> list[tuple[int, str]] | None:
    """None — канал недоступен или не открывает веб-просмотр."""
    try:
        async with aiohttp.ClientSession(headers=HEADERS, timeout=aiohttp.ClientTimeout(total=20)) as http:
            async with http.get(f"https://t.me/s/{username}", allow_redirects=False) as resp:
                if resp.status != 200:
                    return None
                posts = parse_posts(await resp.text())
    except Exception:
        log.info("канал %s не открылся", username)
        return None
    return posts or None


def contact_of(text: str, channel: str) -> str:
    """Первый @ник/ссылка t.me/ник в тексте, кроме самого канала и ботов."""
    for m in _CONTACT.finditer(text):
        name = m.group(1) or m.group(2)
        if name.lower() != channel.lower() and not name.lower().endswith("bot"):
            return name
    return ""


def author_for(contact: str) -> AuthorInfo:
    """Постоянный отрицательный ID по нику: работают чёрный список, антидубли и «🚫 Спам»."""
    return AuthorInfo(user_id=-(10**10 + zlib.crc32(contact.lower().encode())), username=contact, first_name=contact)


async def poll_channel(g: Group, posts: list[tuple[int, str]]) -> int:
    """Новые посты -> общий вход. Первый опрос только запоминает место, старое не разбираем."""
    newest = max((pid for pid, _ in posts), default=0)
    first = g.last_post_id == 0
    handled = 0
    if not first:
        for pid, text in posts:
            if pid <= g.last_post_id or len(text) < 8:
                continue
            contact = contact_of(text, g.username)
            if not contact:
                continue
            lead = await intake.handle(Incoming(
                text=text, author=author_for(contact), group_tg_id=g.tg_id, group_title=g.title,
                message_id=pid, link=links.message_link(g.tg_id, g.username, pid),
            ))
            handled += bool(lead)
    async with Session() as s:
        row = await s.get(Group, g.id)
        if row and newest > row.last_post_id:
            row.last_post_id = newest
            await s.commit()
    return handled


async def poll_all() -> int:
    async with Session() as s:
        channels = (await s.scalars(select(Group).where(
            Group.kind == "channel", Group.status == "joined", Group.username != ""))).all()
    leads = 0
    for g in channels:
        posts = await fetch_posts(g.username)
        if posts:
            leads += await poll_channel(g, posts)
        await asyncio.sleep(random.uniform(2, 5))
    return leads


async def channel_loop() -> None:
    while True:
        minutes = 10
        try:
            await poll_all()
            async with Session() as s:
                minutes = await settings.get_int(s, "channel_poll_min")
        except Exception:
            log.exception("channel_loop")
        await asyncio.sleep(max(minutes, 1) * 60)
