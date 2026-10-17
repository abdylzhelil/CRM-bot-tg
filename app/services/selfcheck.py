"""«🧪 Самопроверка»: бот проверяет сам себя изнутри за несколько секунд.

Ничего не публикует, не платит и никому не пишет — только смотрит: база, настройки, Telegram, разведчики, фоновые задачи,
фильтр заявок на примерах, раздача, ЮKassa (запрос «о магазине»), копия базы, диск, память, сбои за сутки.
"""
import asyncio
import shutil
from datetime import timedelta
from pathlib import Path

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app import config
from app.models import (
    Category, ErrorLog, Group, Keyword, PendingLink, ScoutAccount, User, UserCategory, now,
)
from app.services import offers, settings, sysmem

TASKS: dict[str, asyncio.Task] = {}  # фоновые задачи (заполняет main.py)
OK, WARN, BAD = "✅", "⚠️", "⛔"

# Проверка фильтра «заказчик или исполнитель» на известных примерах (если тут ⛔ — правила сломались)
SAMPLES = [
    ("Ищу кто сделает сайт для салона красоты, бюджет до 40 тысяч", "request"),
    ("Подскажите хорошего дизайнера для логотипа", "request"),
    ("🚀 Нужен сайт под ваш бизнес? Сделаю за 3 дня, от 14000 ₽. Чем занимаюсь: сайты, боты. Примеры работ скину в лс", "offer"),
    ("Делаем лендинги под ключ, портфолио в профиле, обращайтесь", "offer"),
]


async def _timed(coro, seconds: float = 10):
    return await asyncio.wait_for(coro, seconds)


async def run(s: AsyncSession, bot=None, manager=None, yk_api=None) -> list[tuple[str, str, str]]:
    """[(значок, что проверяли, итог/что сделать)]."""
    out: list[tuple[str, str, str]] = []

    def add(icon: str, name: str, note: str) -> None:
        out.append((icon, name, note))

    # база
    try:
        await s.execute(text("SELECT 1"))
        add(OK, "База данных", "отвечает")
    except Exception as e:
        add(BAD, "База данных", f"не отвечает ({type(e).__name__}) — перезапустите бота")
        return out

    # настройки
    bad = []
    from app.models import Setting

    for row in (await s.scalars(select(Setting))).all():
        if row.key in settings.DEFAULTS:
            try:
                settings.validate(row.key, row.value)
            except Exception:
                bad.append(row.key)
    add(BAD if bad else OK, "Настройки", ("неверные значения: " + ", ".join(bad) + " — исправьте в ⚙️") if bad else "все значения в порядке")

    # Telegram
    if bot is not None:
        try:
            me = await _timed(bot.get_me())
            add(OK, "Telegram (бот)", f"@{me.username} на связи")
        except Exception as e:
            add(BAD, "Telegram (бот)", f"нет связи ({type(e).__name__})")

    # разведчики
    accs = (await s.scalars(select(ScoutAccount).where(ScoutAccount.enabled))).all()
    if not accs:
        add(WARN, "Разведчики", "нет включённых аккаунтов — заявки идут только от бота в группах и бота модерации")
    for acc in accs:
        client = manager.clients.get(acc.id) if manager else None
        if client is None:
            add(BAD, f"Разведчик «{acc.name}»", f"не подключён (статус {acc.status}) — 👤 Аккаунты → войти заново")
            continue
        try:
            await _timed(client.get_me())
            add(OK if acc.status in ("ok", "new") else WARN, f"Разведчик «{acc.name}»",
                "на связи" + ("" if acc.status in ("ok", "new") else f", но статус «{acc.status}» (Telegram ограничил)"))
        except Exception as e:
            add(BAD, f"Разведчик «{acc.name}»", f"не отвечает ({type(e).__name__})")

    # вступление в новые группы и поиск
    from app.services import scoutdiag

    level, diag = await scoutdiag.join_diagnosis(s, manager)
    add({"ok": OK, "warn": WARN, "bad": BAD}[level], "Вступление в группы", "; ".join(diag))

    # фоновые задачи
    dead = []
    for name, task in TASKS.items():
        if task.done():
            exc = task.exception() if not task.cancelled() else None
            dead.append(f"{name}" + (f" ({type(exc).__name__})" if exc else ""))
    if TASKS:
        add(BAD if dead else OK, "Фоновые задачи",
            ("остановились: " + ", ".join(dead) + " — перезапустите бота и пришлите «🩺 Отчёт»") if dead else f"работают все ({len(TASKS)})")

    # ниши и слова
    cats = await s.scalar(select(func.count()).select_from(Category).where(Category.enabled)) or 0
    kws = await s.scalar(select(func.count()).select_from(Keyword)) or 0
    add(OK if cats and kws else BAD, "Ниши и слова", f"ниш {cats}, слов {kws}" + ("" if cats and kws else " — без них заявки не находятся"))

    # фильтр на примерах
    wrong = [t[:40] for t, want in SAMPLES if offers.rule_verdict(t) != want]
    add(BAD if wrong else OK, "Фильтр «заказчик или исполнитель»",
        ("ошибся на примерах: " + "; ".join(wrong)) if wrong else f"верно на всех {len(SAMPLES)} примерах")

    # раздача
    people = await s.scalar(select(func.count(func.distinct(UserCategory.user_id))).join(
        User, User.tg_id == UserCategory.user_id).where(User.onboarded, ~User.blocked, ~User.paused)) or 0
    add(OK if people else WARN, "Получатели заявок", f"людей с нишами: {people}" + ("" if people else " — заявки получат только админы"))

    # группы
    joined = await s.scalar(select(func.count()).select_from(Group).where(Group.status == "joined")) or 0
    add(OK if joined else WARN, "Группы", f"разведчик читает групп: {joined}")

    # ЮKassa
    from app.services import payments

    if payments.links_enabled():
        try:
            data = await _timed((yk_api or payments._api)("GET", "/me"))
            code = data.get("_http", 200)
            if code == 200:
                add(OK, "ЮKassa", f"ключи работают (магазин {data.get('account_id', '?')})")
            else:
                add(BAD, "ЮKassa", f"ключи не подходят (ответ {code}) — проверьте YOOKASSA_SHOP_ID/SECRET_KEY в .env")
        except Exception as e:
            add(WARN, "ЮKassa", f"не ответила ({type(e).__name__}) — попробуйте позже")
    else:
        add(WARN, "ЮKassa", "ключей нет в .env — оплата по ссылке выключена")
    stuck = await s.scalar(select(func.count()).select_from(PendingLink).where(
        ~PendingLink.done, PendingLink.created_at < now() - timedelta(hours=4))) or 0
    if stuck:
        add(WARN, "Оплаты по ссылкам", f"{stuck} давно не проверены — фоновая «оплата по ссылкам» могла остановиться")

    # копия базы
    from app.services import backup

    src = backup.sqlite_path()
    if src is not None:
        copies = sorted((src.parent / "backups").glob("*.db"), key=lambda p: p.stat().st_mtime) if (src.parent / "backups").exists() else []
        if not copies:
            add(WARN, "Копия базы", "копий ещё нет (первая появится в течение суток)")
        else:
            from datetime import datetime

            age_h = (datetime.now().timestamp() - copies[-1].stat().st_mtime) / 3600
            add(OK if age_h < 30 else WARN, "Копия базы", f"последняя {age_h:.0f} ч назад, всего {len(copies)}")

    # диск и память
    try:
        free_mb = shutil.disk_usage(Path(config.MEDIA_DIR).parent if Path(config.MEDIA_DIR).parent.exists() else ".").free // 2**20
        add(OK if free_mb > 1000 else (WARN if free_mb > 300 else BAD), "Диск", f"свободно {free_mb} МБ")
    except OSError:
        pass
    m = sysmem.read()
    if m:
        add({"ok": OK, "warn": WARN, "bad": BAD}[m["level"]], "Память", f"свободно {m['available_mb']} из {m['total_mb']} МБ")

    # сбои
    errs = await s.scalar(select(func.count()).select_from(ErrorLog).where(ErrorLog.created_at > now() - timedelta(days=1))) or 0
    add(OK if not errs else WARN, "Сбои за сутки", f"{errs}" + (" — подробности в «🩺 Отчёт о проблемах»" if errs else ""))

    # ИИ (информация)
    from app.services import ai

    add(OK if ai.enabled() else "⚪️", "ИИ-помощник", "ключ есть" if ai.enabled() else "без ключа — работаем по бесплатным правилам")
    return out


def render(rows: list[tuple[str, str, str]]) -> str:
    bad = sum(1 for r in rows if r[0] == BAD)
    warn = sum(1 for r in rows if r[0] == WARN)
    head = "🧪 <b>Самопроверка</b>: " + ("всё в порядке ✅" if not bad and not warn else f"проблем {bad}, предупреждений {warn}")
    return head + "\n\n" + "\n".join(f"{i} <b>{n}</b>: {note}" for i, n, note in rows)
