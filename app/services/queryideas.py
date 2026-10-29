"""🤖 ИИ подсказывает поисковые запросы для разведчика (владелец 29.10: «пусть ИИ тоже помогает»).

Раз в неделю для каждого круга поиска (свой регион → соседи/Кавказ → Россия) ИИ предлагает короткие запросы:
районы, сёла, местные названия групп («барахолка Каспийск», «объявления Кизилюртовский район»…). Ручные и
автоматические запросы остаются — подсказки только добавляются в конец своего круга. Без ИИ ничего не меняется.
В ИИ уходят только названия мест и ниш, никаких данных людей.
"""
import json
import logging
import re
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import now
from app.services import settings

log = logging.getLogger(__name__)

KEY = "ai_queries"
EVERY = timedelta(days=7)
PER_TIER = 15
SYSTEM = (
    "Ты помогаешь найти открытые Telegram-группы, где жители пишут объявления и ищут мастеров и услуги. "
    "По списку мест предложи короткие поисковые запросы (1–4 слова, по-русски), какими такие группы обычно называются: "
    "город/район/село + «объявления», «барахолка», «услуги», «работа», «чат», «мастера» и т. п. Используй и районы, "
    "сёла и посёлки этих мест. Не предлагай флуд, взаимопиар, рекламу, знакомства, игры, заработок, крипту. "
    "Ответ JSON: queries — список строк."
)
_OK = re.compile(r"^[а-яё0-9 \-]{2,40}$", re.I)


def clean(items: list) -> list[str]:
    from app.services import junk

    out: list[str] = []
    for q in items or []:
        q = re.sub(r"\s+", " ", str(q).strip().strip("«»\"'.,")).strip()
        if not _OK.match(q) or len(q.split()) > 4 or junk.is_junk_title(q):
            continue
        if q.casefold() not in {x.casefold() for x in out}:
            out.append(q)
    return out[:PER_TIER]


async def stored(s: AsyncSession) -> list[list[str]]:
    try:
        data = json.loads(await settings.raw(s, KEY) or "{}")
        tiers = [clean(x) for x in data.get("tiers", [])][:3]
    except (ValueError, TypeError, AttributeError):
        tiers = []
    return tiers + [[] for _ in range(3 - len(tiers))]


async def refresh(s: AsyncSession, places: list[list[str]], niches: list[str], force: bool = False) -> int:
    """Обновить подсказки, если пора (раз в неделю). places — места каждого круга. → сколько запросов получено."""
    from app.services import brain

    try:
        data = json.loads(await settings.raw(s, KEY) or "{}")
    except ValueError:
        data = {}
    at = data.get("at", "")
    if not force and at and at > (now() - EVERY).isoformat():
        return 0
    tiers: list[list[str]] = []
    for i, pl in enumerate(places[:3]):
        if not pl:
            tiers.append([])
            continue
        text = "Места: " + ", ".join(pl[:25]) + "\nНиши (для идей): " + ", ".join(niches[:20])
        ans = await brain.ask(s, "queries", SYSTEM, text, brain.obj({"queries": brain.STRS}), max_tokens=500)
        if ans is None and i == 0:
            return 0  # ИИ недоступен — ничего не меняем, попробуем в следующий раз
        tiers.append(clean((ans or {}).get("queries", [])))
    await settings.put(s, KEY, json.dumps({"at": now().isoformat(), "tiers": tiers}, ensure_ascii=False))
    await s.commit()
    return sum(len(x) for x in tiers)
