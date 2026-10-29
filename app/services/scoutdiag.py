"""Почему разведчик не вступает в новые группы — коротко и по-человечески (для 🔍 Группы и «🧪 Самопроверки»)."""
from datetime import date

from sqlalchemy import func, select

from app.models import Group, ScoutAccount
from app.services import settings


async def join_diagnosis(s, manager=None) -> tuple[str, list[str]]:
    """(уровень ok/warn/bad, строки). Каждая проблема — с подсказкой, какую кнопку нажать."""
    lines, level = [], "ok"

    def bad(text):
        nonlocal level
        level = "bad"
        lines.append("🔴 " + text)

    def warn(text):
        nonlocal level
        level = "warn" if level == "ok" else level
        lines.append("⚠️ " + text)

    if await settings.get(s, "scout_paused") == "1":
        bad("разведчик на паузе — 🔍 Группы → 🔍 Поиск и вступление → «▶️ Снять с паузы»")
    accs = (await s.scalars(select(ScoutAccount).where(ScoutAccount.enabled))).all()
    if not accs:
        bad("нет включённых аккаунтов — 🛠 Управление → 👤 Аккаунты")
    connected = set(manager.clients) if manager is not None else None
    today = date.today().isoformat()
    for acc in accs:
        total = await s.scalar(select(func.count()).select_from(Group).where(
            Group.account_id == acc.id, Group.status == "joined")) or 0
        joined = acc.joined_today if acc.joined_date == today else 0
        today = (f"сегодня вступил {joined} (предел теперь {acc.daily_join_limit} — новый действует с завтра)"
                 if joined > acc.daily_join_limit else f"сегодня вступил {joined} из {acc.daily_join_limit}")
        line = f"«{acc.name}»: {today}, всего групп {total} из {acc.max_groups}"
        if connected is not None and acc.id not in connected:
            bad(line + " — НЕ ПОДКЛЮЧЁН, войдите заново: 👤 Аккаунты")
        elif acc.status in ("limited", "dead"):
            bad(line + f" — Telegram ограничил аккаунт (статус {acc.status})")
        elif total >= acc.max_groups:
            bad(line + " — достигнут предел групп: «🔄 Ротация сейчас» или увеличьте предел в 👤 Аккаунты")
        elif joined >= acc.daily_join_limit:
            lines.append("✅ " + line + " — дневной предел, завтра продолжит")
        else:
            lines.append("✅ " + line)
    cnt = dict((await s.execute(select(Group.status, func.count()).where(Group.kind == "group")
                                .group_by(Group.status))).all())
    queue, cands = cnt.get("approved", 0), cnt.get("candidate", 0)
    from app.scout import joiner, search as search_

    st = await search_.stage(s)
    home = await settings.get(s, "home_region")
    lines.append(f"🧭 этап поиска: {search_.STAGE_LABELS[st]}")
    if await settings.get(s, "scout_grow") != "1":
        bad("поиск и вступление в новые группы выключены — 🔍 Группы → 🧹 Качество групп → 🧹 Навести порядок → "
            "«▶️ Включить поиск и вступление»")
    ready = len(joiner.joinable((await s.scalars(select(Group).where(
        Group.status == "approved", Group.kind == "group").limit(300))).all(), home, st)) if queue else 0
    if queue and not ready:
        warn(f"в очереди {queue}, но все — из других мест (не {home}) и ждут следующего этапа поиска")
    elif queue:
        lines.append(f"⏳ в очереди на вступление: {queue}" + (f" (сейчас подходят {ready})" if ready < queue else ""))
    if cands and await settings.get(s, "mode") != "auto":
        warn(f"{cands} найденных групп ждут вашего отбора — 🔍 Группы → «❓ Кандидаты» "
             "(или ⚙️ → 🔍 Разведчик → Режим вступления = auto, тогда вступает сам)")
    if not queue and not cands:
        warn("очередь пуста и новых кандидатов нет — поиск не находит новых групп. "
             "Нажмите «▶️ Искать сейчас» или добавьте запросы в «🔎 Запросы» (🔍 Поиск и вступление)")
    from app.scout.search import last_search

    lines.append(f"🔎 последний поиск: {await last_search()}")
    return level, lines
