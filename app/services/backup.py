"""Ежедневная копия базы SQLite (оплаты и пользователи — важные данные). Для PostgreSQL бэкап делает сервер."""
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
    for old in sorted(folder.glob(f"{src.stem}-*.db"))[:-keep]:
        old.unlink()
    return dest


async def send_latest(bot, admin_ids, folder: Path | None = None) -> int:
    """Прислать свежую копию базы администраторам файлом (раз в неделю) — чтобы не скачивать с сервера руками."""
    from aiogram.types import FSInputFile

    src = sqlite_path()
    folder = folder or (src.parent / "backups" if src else None)
    files = sorted(folder.glob("*.db")) if folder and folder.exists() else []
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
    src = sqlite_path()
    if src is None:
        return
    while True:
        try:
            if src.exists():
                await asyncio.to_thread(make_backup, src)
                await _weekly_send()
        except Exception:
            log.exception("backup_loop")
        await asyncio.sleep(24 * 3600)
