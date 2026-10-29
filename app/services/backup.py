"""Ежедневная копия базы (оплаты и пользователи — важные данные) в data/backups, 7 штук, и раз в неделю — файлом владельцу.

SQLite копируется своим backup API. PostgreSQL (после переноса базы) — выгрузкой всех таблиц в такой же файл SQLite
(app/dbcopy.py): копия остаётся одним файлом, её так же можно открыть и из неё же восстановить базу.
"""
import asyncio
import logging
import sqlite3
from datetime import date, datetime
from pathlib import Path

from app import config

log = logging.getLogger(__name__)
KEEP = 7


def sqlite_path() -> Path | None:
    url = config.DATABASE_URL
    if not url.startswith("sqlite") or ":memory:" in url:
        return None
    return Path(url.split("///", 1)[1])


def backup_folder() -> Path:
    src = sqlite_path()
    return (src.parent if src else Path("data")) / "backups"


def _rotate(folder: Path, stem: str, keep: int = KEEP) -> None:
    for old in sorted(folder.glob(f"{stem}-*.db"))[:-keep]:
        old.unlink()


async def backup_once() -> Path | None:
    """Сделать копию текущей базы (любой). None — базы в файле нет (тесты в памяти и т. п.)."""
    src = sqlite_path()
    if src is not None:
        return await asyncio.to_thread(make_backup, src) if src.exists() else None
    if not config.DATABASE_URL.startswith("postgresql"):
        return None
    from app import dbcopy

    folder = backup_folder()
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / f"crm-{date.today().isoformat()}.db"
    tmp = dest.with_suffix(".part")
    tmp.unlink(missing_ok=True)
    await dbcopy.copy_all(config.DATABASE_URL, f"sqlite+aiosqlite:///{tmp}")
    tmp.replace(dest)  # файл появляется целиком, недоделанная копия не попадёт в рассылку
    _rotate(folder, "crm")
    return dest


def make_backup(src: Path, folder: Path | None = None, keep: int = KEEP) -> Path:
    """Согласованная копия через sqlite3 backup API (безопасно при работающем боте); старые удаляются."""
    folder = folder or src.parent / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / f"{src.stem}-{date.today().isoformat()}.db"
    source, target = sqlite3.connect(src), sqlite3.connect(dest)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    _rotate(folder, src.stem, keep)
    return dest


async def send_latest(bot, admin_ids, folder: Path | None = None) -> int:
    """Прислать свежую копию базы администраторам файлом (раз в неделю) — чтобы не скачивать с сервера руками."""
    from aiogram.types import FSInputFile

    folder = folder or backup_folder()
    files = sorted(folder.glob("*.db"), key=lambda p: p.stat().st_mtime) if folder.exists() else []
    if not files:
        return 0
    sent = 0
    for uid in admin_ids:
        try:
            await bot.send_document(uid, FSInputFile(files[-1]), caption=(
                "💾 Еженедельная копия базы «КлиентРадара». Сохраните файл у себя. "
                "В нём данные пользователей — никому не пересылайте."))
            sent += 1
        except Exception:
            log.warning("не удалось отправить копию базы %s", uid, exc_info=True)
    return sent


async def _weekly_send() -> None:
    """Раз в 7 дней отправить копию администраторам (дата последней отправки — в настройках)."""
    from datetime import timedelta

    from app.bot import notify
    from app.db import Session
    from app.models import Setting, now

    async with Session() as s:
        row = await s.get(Setting, "backup_sent_at")
        if row is not None and datetime.fromisoformat(row.value) > now() - timedelta(days=7):
            return
        if await send_latest(notify._bot, sorted(config.OWNER_IDS)):
            if row is None:
                row = Setting(key="backup_sent_at", value="")
                s.add(row)
            row.value = now().isoformat()
            await s.commit()


async def backup_loop() -> None:
    if sqlite_path() is None and not config.DATABASE_URL.startswith("postgresql"):
        return
    while True:
        try:
            if await backup_once() is not None:
                await _weekly_send()
        except Exception:
            log.exception("backup_loop")
        await asyncio.sleep(24 * 3600)
