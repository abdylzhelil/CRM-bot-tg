"""Перенос базы целиком: SQLite → PostgreSQL (переезд в Россию) и обратно PostgreSQL → файл SQLite (ежедневная копия).

Запуск на сервере (бот должен быть остановлен, чтобы данные не менялись во время переноса):
    docker compose stop bot
    docker compose run --rm bot python -m app.dbcopy --to "postgresql+asyncpg://crm:ПАРОЛЬ@АДРЕС:5432/crm"
Источник по умолчанию — текущая база из DATABASE_URL. Целевая база должна быть пустой (иначе отказ, ключ --force — очистить).

Что делает: создаёт таблицы, копирует строки по порядку связей (сначала «родители»), обрезает слишком длинные строки
(SQLite длину не проверял, PostgreSQL проверяет), «висячие» ссылки на удалённые записи обнуляет или пропускает строку,
выставляет счётчики id в PostgreSQL. В конце сверяет число строк в каждой таблице.
"""
import argparse
import asyncio
import logging

from sqlalchemy import Integer, String, Text, func, inspect, select, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.models import Base

log = logging.getLogger(__name__)
BATCH = 500


def _fix_row(table, row: dict, present: dict[str, set], report: dict) -> dict | None:
    """Подогнать строку под строгую базу. None — строку пропустить (ссылка на несуществующую обязательную запись)."""
    for column in table.columns:
        value = row.get(column.name)
        if isinstance(value, str) and isinstance(column.type, String) and not isinstance(column.type, Text):
            limit = column.type.length
            if limit and len(value) > limit:
                row[column.name] = value[:limit]
                report["обрезано строк"] = report.get("обрезано строк", 0) + 1
        for fk in column.foreign_keys:
            if value is None:
                continue
            parent = fk.column.table.name
            if parent in present and value not in present[parent]:
                if column.nullable:
                    row[column.name] = None
                    report["обнулено ссылок"] = report.get("обнулено ссылок", 0) + 1
                else:
                    report["пропущено строк"] = report.get("пропущено строк", 0) + 1
                    return None
    return row


async def copy_all(src_url: str, dst_url: str, force: bool = False) -> dict:
    src = create_async_engine(src_url)
    dst = create_async_engine(dst_url)
    report: dict = {}
    counts: dict[str, tuple[int, int]] = {}
    try:
        async with dst.begin() as conn:
            if force:
                await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
        async with dst.connect() as conn:
            busy = [t.name for t in Base.metadata.sorted_tables
                    if await conn.scalar(select(func.count()).select_from(t))]
        if busy:
            raise SystemExit(f"Целевая база не пустая ({', '.join(busy[:5])}…). Нужна пустая база или ключ --force.")

        async with src.connect() as sconn:
            src_tables = set(await sconn.run_sync(lambda c: inspect(c).get_table_names()))
            src_cols = {t: {c["name"] for c in await sconn.run_sync(lambda c, t=t: inspect(c).get_columns(t))}
                        for t in src_tables}
            present: dict[str, set] = {}  # id уже перенесённых строк — для проверки ссылок
            for table in Base.metadata.sorted_tables:
                if table.name not in src_tables:
                    counts[table.name] = (0, 0)
                    continue
                cols = [c for c in table.columns if c.name in src_cols[table.name]]
                pk = list(table.primary_key.columns)
                track = len(pk) == 1
                present[table.name] = set()
                total = done = 0
                result = await sconn.stream(select(*cols).order_by(*pk))  # по id: «родитель» в той же таблице — раньше
                self_ref = any(fk.column.table is table for c in table.columns for fk in c.foreign_keys)
                async for chunk in result.mappings().partitions(BATCH):
                    rows = []
                    if track and self_ref:  # ссылки внутри порции (подниша → ниша) — не считать «висячими»
                        present[table.name].update(r[pk[0].name] for r in chunk)
                    for r in chunk:
                        total += 1
                        row = _fix_row(table, dict(r), present, report)
                        if row is not None:
                            rows.append(row)
                    if rows:
                        async with dst.begin() as dconn:
                            await dconn.execute(table.insert(), rows)
                        done += len(rows)
                        if track:
                            present[table.name].update(r[pk[0].name] for r in rows)
                counts[table.name] = (total, done)

        if dst.dialect.name == "postgresql":  # счётчики id: следующая запись получит id больше перенесённых
            async with dst.begin() as conn:
                for table in Base.metadata.sorted_tables:
                    pk = list(table.primary_key.columns)
                    if len(pk) == 1 and isinstance(pk[0].type, Integer) and pk[0].autoincrement in (True, "auto"):
                        seq = await conn.scalar(text("SELECT pg_get_serial_sequence(:t, :c)"),
                                                {"t": table.name, "c": pk[0].name})
                        if seq:
                            await conn.execute(text(
                                f"SELECT setval('{seq}', COALESCE((SELECT MAX({pk[0].name}) FROM {table.name}), 1), "
                                f"(SELECT MAX({pk[0].name}) FROM {table.name}) IS NOT NULL)"))
        async with dst.connect() as conn:
            for table in Base.metadata.sorted_tables:
                have = await conn.scalar(select(func.count()).select_from(table))
                if have != counts.get(table.name, (0, 0))[1]:
                    raise RuntimeError(f"Сверка не сошлась: {table.name} — перенесено {counts[table.name][1]}, в базе {have}")
    finally:
        await src.dispose()
        await dst.dispose()
    return {"tables": counts, "fixes": report}


def main() -> None:
    from app import config

    p = argparse.ArgumentParser(description="Перенос базы CRM-бота")
    p.add_argument("--from", dest="src", default=config.DATABASE_URL, help="откуда (по умолчанию — текущая база)")
    p.add_argument("--to", dest="dst", required=True, help="куда, например postgresql+asyncpg://crm:пароль@адрес:5432/crm")
    p.add_argument("--force", action="store_true", help="очистить целевую базу перед переносом")
    args = p.parse_args()
    res = asyncio.run(copy_all(args.src, args.dst, args.force))
    moved = sum(d for _, d in res["tables"].values())
    print(f"✅ Перенесено строк: {moved} в {len(res['tables'])} таблицах. Сверка числа строк — сошлась.")
    for name, (total, done) in res["tables"].items():
        if total:
            print(f"  {name}: {done}" + (f" из {total}" if done != total else ""))
    for k, v in res["fixes"].items():
        print(f"  ⚠️ {k}: {v}")


if __name__ == "__main__":
    main()
