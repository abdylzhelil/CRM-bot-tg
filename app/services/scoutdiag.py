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
        bad("разведчик на паузе — 🔍 Группы → «▶️ Возобновить разведчика»")
    accs = (await s.scalars(select(ScoutAccount).where(ScoutAccount.enabled))).all()
    if not accs:
        bad("нет включённых аккаунтов — 🛠 Управление → 👤 Аккаунты")
    connected = set(manager.clients) if manager is not None else None
    today = date.today().isoformat()
    for acc in accs:
        total = await s.scalar(select(func.count()).select_from(Group).where(
            Group.account_id == acc.id, Group.status == "joined")) or 0
        joined = acc.joined_today if acc.joined_date == today else 0
        line = f"«{acc.name}»: сегодня вступил {joined} из {acc.daily_join_limit}, всего групп {total} из {acc.max_groups}"
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
    if queue:
        lines.append(f"⏳ в очереди на вступление: {queue}")
    elif cands:
        warn(f"очередь пуста, а {cands} найденных групп ждут вашего отбора — 🔍 Группы → «❓ Кандидаты» "
             "(или ⚙️ → Режим вступления = auto, тогда вступает сам)")
    else:
        warn("очередь пуста и новых кандидатов нет — поиск не находит новых групп. "
             "Нажмите «▶️ Искать сейчас» или добавьте запросы в «🔎 Поисковые запросы»")
    from app.scout.search import last_search

    lines.append(f"🔎 последний поиск: {await last_search()}")
    return level, lines
