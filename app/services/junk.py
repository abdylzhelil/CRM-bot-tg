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
_TITLE = re.compile(
    r"реклам|пиар|взаимо|\bpr\b|флуд|ролк|ролев|\bрп\b|анкет|дорам|аниме|фандом|\bкф\b", re.I)


def is_junk_title(title: str) -> bool:
    return bool(_TITLE.search(title or ""))


async def mark_existing(s: AsyncSession) -> int:
    """Один раз отметить уже вступившие «мусорные» группы. Коммит — здесь."""
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
