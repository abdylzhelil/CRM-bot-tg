from pathlib import Path

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app import config
from app.models import Base

if config.DATABASE_URL.startswith("sqlite"):
    Path("data").mkdir(exist_ok=True)


def make_engine(url: str) -> AsyncEngine:
    """Движок БД. Для SQLite включаем режим WAL: бот, фоновые задачи и кабинет пишут в одну базу одновременно,
    без WAL читатели и писатель мешают друг другу (ошибка «database is locked»); busy_timeout — ждать освобождения, а не падать."""
    if url.startswith("postgresql"):
        # база на другом сервере (Россия), бот в Алматы: связь может рваться — проверять соединение перед запросом
        # и не держать старые соединения дольше 30 минут
        engine = create_async_engine(url, pool_pre_ping=True, pool_recycle=1800, pool_size=5, max_overflow=5)
    else:
        engine = create_async_engine(url)
    if url.startswith("sqlite") and ":memory:" not in url:
        @event.listens_for(engine.sync_engine, "connect")
        def _sqlite_pragmas(dbapi_connection, _record) -> None:
            cur = dbapi_connection.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")  # с WAL безопасно и быстрее; при сбое питания теряется максимум последняя запись
            cur.execute("PRAGMA busy_timeout=15000")
            cur.close()
    return engine


engine = make_engine(config.DATABASE_URL)
Session: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
