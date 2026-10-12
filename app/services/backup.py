"""Ежедневная копия базы SQLite (оплаты и пользователи — важные данные). Для PostgreSQL бэкап делает сервер."""
import asyncio
import logging
import sqlite3
from datetime import date
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


async def backup_loop() -> None:
    src = sqlite_path()
    if src is None:
        return
    while True:
        try:
            if src.exists():
                await asyncio.to_thread(make_backup, src)
        except Exception:
            log.exception("backup_loop")
        await asyncio.sleep(24 * 3600)
