"""Обновление базы без потери данных.

create_all создаёт только новые таблицы, поэтому новые колонки в старых таблицах добавляем сами
(ALTER TABLE ... ADD COLUMN), а затем переносим данные однопользовательской версии на владельца.
"""
import logging

from sqlalchemy import inspect, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app import config
from app.models import Base, Category, Client, Delivery, Lead, Setting, User, UserCategory

log = logging.getLogger(__name__)


def _default_sql(column, dialect) -> str:
    default = column.default.arg if column.default is not None and column.default.is_scalar else None
    if default is None:
        return ""
    if isinstance(default, bool):
        return " DEFAULT " + (("TRUE" if default else "FALSE") if dialect.name == "postgresql" else str(int(default)))
    if isinstance(default, int | float):
        return f" DEFAULT {default}"
    return " DEFAULT '" + str(default).replace("'", "''") + "'"


def _add_missing_columns(sync_conn) -> list[str]:
    insp = inspect(sync_conn)
    dialect = sync_conn.dialect
    added = []
    for table in Base.metadata.sorted_tables:
        if not insp.has_table(table.name):
            continue
        existing = {c["name"] for c in insp.get_columns(table.name)}
        for column in table.columns:
            if column.name in existing:
                continue
            ddl = (
                f'ALTER TABLE {table.name} ADD COLUMN {column.name} '
                f'{column.type.compile(dialect=dialect)}{_default_sql(column, dialect)}'
            )
            sync_conn.execute(text(ddl))
            added.append(f"{table.name}.{column.name}")
    return added


async def upgrade_schema(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        added = await conn.run_sync(_add_missing_columns)
    if added:
        log.info("добавлены колонки: %s", ", ".join(added))


async def migrate_data(s: AsyncSession) -> None:
    """Владельцы из OWNER_IDS — администраторы; их старые клиенты и заявки остаются у них."""
    owners = sorted(config.OWNER_IDS)
    if not owners:
        return
    main_owner = owners[0]
    roots = (await s.scalars(select(Category.id).where(Category.parent_id.is_(None)))).all()
    for uid in owners:
        user = await s.get(User, uid)
        if user is None:
            user = User(tg_id=uid, is_admin=True, plan="full", onboarded=True)
            s.add(user)
            if not await s.scalar(select(UserCategory).where(UserCategory.user_id == uid)):
                s.add_all(UserCategory(user_id=uid, category_id=cid) for cid in roots)
        user.is_admin = True

    if await s.get(Setting, "migrated_multiuser") is None:
        await s.execute(Client.__table__.update().where(Client.user_id == 0).values(user_id=main_owner))
        delivered = select(Delivery.lead_id)
        for lead in (await s.scalars(select(Lead).where(Lead.id.not_in(delivered)))).all():
            s.add(Delivery(lead_id=lead.id, user_id=main_owner, status=lead.status, sent=True,
                           deliver_after=lead.created_at, created_at=lead.created_at))
        quiet = await s.get(Setting, "quiet_hours")
        if quiet and quiet.value:
            (await s.get(User, main_owner)).quiet_hours = quiet.value
        s.add(Setting(key="migrated_multiuser", value="1"))
    await s.commit()
