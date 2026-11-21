"""Группы «без заявок»: рекламные свалки, взаимопиар, флуды и ролевые.

Там почти все сообщения — реклама или приглашения, настоящих заказчиков нет, а мусор приходит как заявки.
Такие группы разведчик не берёт при поиске (не тратит вступления), а у уже вступивших заявки не создаются
(`groups.no_leads`). Для рекламы разведчиком группа остаётся доступной. Вернуть: 🔍 Группы → «🙈 Без заявок».
"""
import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Group, Setting

KEY = "junk_groups_v1"  # уже вступившие группы отмечены один раз; дальше решает админ
QUEUE_KEY = "junk_queue_v1"  # очередь на вступление почищена один раз
_TITLE = re.compile(
    r"реклам|пиар|взаимо|\bpr\b|флуд|ролк|ролев|\bрп\b|анкет|дорам|аниме|фандом|\bкф\b|фанфик|"
    r"genshin|геншин|minecraft|майнкрафт|roblox|роблокс|standoff|стандофф|brawl|бравл|k-?pop|кпоп|\bbts\b|"
    r"знакомств|вписк|\b18\+", re.I)
MIN_TEXTS = 10       # меньше сообщений — по содержанию не судим
JUNK_SHARE = 0.75    # столько и больше рекламы/приглашений — группа-свалка
FOREIGN_SHARE = 0.7  # столько и больше сообщений не по-русски — заявки нашим пользователям оттуда не придут


def is_junk_title(title: str) -> bool:
    return bool(_TITLE.search(title or ""))


def _cyrillic(text: str) -> bool:
    letters = [c for c in text.lower() if c.isalpha()]
    return bool(letters) and sum("а" <= c <= "я" or c == "ё" for c in letters) >= len(letters) / 3


def content_reason(texts: list[str]) -> str:
    """Почему группа не подходит по последним сообщениям ("" — подходит или судить рано). Бесплатно, без ИИ."""
    from app.services import offers

    texts = [x for x in texts if len((x or "").split()) >= 5]
    if len(texts) < MIN_TEXTS:
        return ""
    foreign = sum(not _cyrillic(x) for x in texts)
    if foreign >= len(texts) * FOREIGN_SHARE:
        return f"сообщения не на русском ({foreign} из {len(texts)})"
    junk = sum(offers.rule_verdict(x) in ("offer", "other") for x in texts)
    if junk >= len(texts) * JUNK_SHARE:
        return f"почти одна реклама и приглашения ({junk} из {len(texts)})"
    return ""


def title_reason(title: str) -> str:
    return "по названию — реклама/флуд/игры" if is_junk_title(title) else ""


async def mark_existing(s: AsyncSession) -> int:
    """Один раз отметить уже вступившие «мусорные» группы. Коммит — здесь."""
    if await s.get(Setting, QUEUE_KEY) is None:  # очередь и кандидаты: «мусорные» по названию не вступаем
        for g in (await s.scalars(select(Group).where(Group.status.in_(("approved", "candidate", "requested")),
                                                       ~Group.catalog))).all():
            if is_junk_title(g.title):
                g.status, g.skip_reason = "skipped", title_reason(g.title)
        s.add(Setting(key=QUEUE_KEY, value="1"))
        await s.commit()
    if await s.get(Setting, KEY) is not None:
        return 0
    n = 0
    for g in (await s.scalars(select(Group).where(Group.status == "joined", ~Group.no_leads, ~Group.catalog))).all():
        if is_junk_title(g.title):
            g.no_leads = True
            n += 1
    s.add(Setting(key=KEY, value="1"))
    await s.commit()
    return n
