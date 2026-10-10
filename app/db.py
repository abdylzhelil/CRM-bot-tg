from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app import config
from app.models import Base

if config.DATABASE_URL.startswith("sqlite"):
    Path("data").mkdir(exist_ok=True)

engine = create_async_engine(config.DATABASE_URL)
Session: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
