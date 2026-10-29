"""Отчёт «почему разведчик не находит и не вступает в группы» — запускается на сервере:

    docker compose exec bot python -m app.scoutreport

Только цифры и названия групп (публичные), без людей. Ничего не меняет.
"""
import asyncio
from collections import Counter
from datetime import timedelta

from sqlalchemy import func, select

from app.db import Session
from app.models import Group, now
from app.services import geo, scoutdiag, settings


async def build() -> str:
    from app.scout import joiner, search
    from app.services import queryideas

    out = []
    async with Session() as s:
        _, lines = await scoutdiag.join_diagnosis(s)
        out += ["== Состояние =="] + lines
        keys = ("mode", "scout_grow", "scout_places", "home_region", "min_members", "min_msgs_per_day",
                "min_members_home", "min_msgs_home", "join_check", "search_interval_h")
        out += ["", "== Настройки =="] + [f"{k} = {await settings.get(s, k)!r}" for k in keys]
        st = await search.stage(s)
        tiers = await search.tier_queries(s)
        ideas = await queryideas.stored(s)
        out += ["", f"== Поиск == этап {st} ({search.STAGE_LABELS[st]}); запросов по кругам: "
                    f"свой {len(tiers[0])}, соседи {len(tiers[1])}, Россия {len(tiers[2])}; "
                    f"подсказок ИИ: {sum(len(x) for x in ideas)}"]
        for i, key in enumerate(search.TIER_KEYS):
            out.append(f"  {key} = {await settings.raw(s, key, '0')}")
        cnt = dict((await s.execute(select(Group.status, func.count()).where(Group.kind == "group")
                                    .group_by(Group.status))).all())
        out += ["", "== Группы по статусам == " + ", ".join(f"{k}: {v}" for k, v in sorted(cnt.items()))]
        day = now() - timedelta(days=3)
        fresh = (await s.scalars(select(Group).where(Group.kind == "group", Group.found_at > day))).all()
        why = Counter((g.skip_reason or "маленькая/тихая") if g.status == "skipped" else g.status for g in fresh)
        out += ["", f"== Найдено за 3 дня: {len(fresh)} =="] + [f"  {n} × {r}" for r, n in why.most_common(12)]
        home = await settings.get(s, "home_region")
        queue = (await s.scalars(select(Group).where(Group.status == "approved", Group.kind == "group")
                                 .limit(300))).all()
        ok = {g.id for g in joiner.joinable(queue, home, st)}
        out += ["", f"== Очередь: {len(queue)}, можно вступать сейчас: {len(ok)} =="]
        for g in queue[:20]:
            city, region = (g.city, g.region) if (g.city or g.region) else geo.title_place(g.title)
            out.append(f"  {'✅' if g.id in ok else '⏳'} {g.title[:50]} · {g.members} уч. · "
                       f"{city or region or 'место ?'}")
        small = [g for g in fresh if g.status == "skipped" and not g.skip_reason][:10]
        if small:
            out += ["", "== Примеры «маленьких/тихих» =="]
            out += [f"  {g.title[:50]} · {g.members} уч. · {g.msgs_per_day:.0f} сообщ./сут · "
                    f"{g.city or g.region or 'место ?'}" for g in small]
    return "\n".join(out)


if __name__ == "__main__":
    print(asyncio.run(build()))
