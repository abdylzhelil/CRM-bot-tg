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
        engine = create_async_engine(url, pool_pre_ping=True, pool_recycle=1800, pool_size=5, max_overflow=15)
    elif url.startswith("sqlite") and ":memory:" not in url:
        # подключения к файлу SQLite дешёвые; запас на случай, если какая-то задача долго ждёт Telegram (21.10: «QueuePool limit»)
        engine = create_async_engine(url, pool_size=10, max_overflow=20, pool_timeout=60)
    else:
        engine = create_async_engine(url)
    if url.startswith("sqlite") and ":memory:" not in url:
        @event.listens_for(engine.sync_engine, "connect")
        def _sqlite_pragmas(dbapi_connection, _record) -> None:
            cur = dbapi_connection.cursor()
            # сначала «ждать до 15 с, если база занята» — иначе следующая команда при занятой базе (копия базы,
            # запись из другой задачи) падала сразу с «database is locked», ещё до ожидания (тревога reminder_loop 21.10)
            cur.execute("PRAGMA busy_timeout=15000")
            cur.execute("PRAGMA journal_mode")
            if (cur.fetchone() or [""])[0].lower() != "wal":  # режим WAL хранится в самом файле базы — включаем один раз
                cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")  # с WAL безопасно и быстрее; при сбое питания теряется максимум последняя запись
            cur.close()
    return engine


engine = make_engine(config.DATABASE_URL)
Session: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
