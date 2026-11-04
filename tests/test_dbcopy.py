"""Перенос базы целиком (app/dbcopy.py): SQLite → другая база и обратно, с починкой того, что строгая база не примет."""
import os

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import dbcopy
from app.models import Base, Category, Group, Lead, Setting, User


async def _fill(url: str) -> None:
    from app.seed import seed

    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as s:
        await seed(s)
        s.add(User(tg_id=5, name="Тест", city="Москва"))
        s.add(Group(id=7, tg_id=-7, title="Группа"))
        await s.flush()
        s.add(Lead(author_id=1, text="Ищу сайт", text_hash="a", group_id=7, group_title="Группа"))
        s.add(Lead(author_id=2, text="Ищу бота", text_hash="b", group_id=999, group_title="удалена"))  # висячая ссылка
        await s.commit()
    async with engine.begin() as conn:  # SQLite пропустил бы слишком длинную строку — PostgreSQL нет
        await conn.execute(Setting.__table__.insert().values(key="long", value="x" * 5000))
    await engine.dispose()


async def _count(url, model):
    engine = create_async_engine(url)
    async with engine.connect() as conn:
        n = await conn.scalar(select(func.count()).select_from(model))
    await engine.dispose()
    return n


@pytest.mark.parametrize("target", ["sqlite", "postgres"])
async def test_copy_whole_database(tmp_path, target):
    src = f"sqlite+aiosqlite:///{tmp_path / 'src.db'}"
    if target == "postgres":
        dst = os.getenv("TEST_DATABASE_URL")
        if not dst:
            pytest.skip("нет TEST_DATABASE_URL — PostgreSQL проверяется отдельно")
    else:
        dst = f"sqlite+aiosqlite:///{tmp_path / 'dst.db'}"
    await _fill(src)
    res = await dbcopy.copy_all(src, dst, force=True)
    assert res["tables"]["leads"] == (2, 2) and res["fixes"].get("обнулено ссылок", 0) >= 1
    assert await _count(dst, Category) == await _count(src, Category) > 70
    engine = create_async_engine(dst)
    async with async_sessionmaker(engine)() as s:
        lead = await s.scalar(select(Lead).where(Lead.text_hash == "b"))
        assert lead.group_id is None  # ссылка на удалённую группу обнулена, заявка сохранена
        assert (await s.get(User, 5)).city == "Москва"
        sub = await s.scalar(select(Category).where(Category.parent_id.is_not(None)))
        assert sub is not None  # подниши сохранили связь с нишей
        s.add(Lead(author_id=3, text="новая", text_hash="c"))  # счётчик id продолжается после перенесённых
        await s.commit()
    await engine.dispose()
    with pytest.raises(SystemExit):  # в непустую базу без --force не льём
        await dbcopy.copy_all(src, dst)
    # обратно: из новой базы в файл SQLite (так будет делаться ежедневная копия)
    back = f"sqlite+aiosqlite:///{tmp_path / 'back.db'}"
    res = await dbcopy.copy_all(dst, back)
    assert res["tables"]["leads"][1] == 3
