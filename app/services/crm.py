"""CRM пользователя: клиенты, этапы, заметки, напоминания — общая логика для бота и мини-приложения."""
from datetime import timedelta

from sqlalchemy import func, or_, select

from app.models import STAGE_NAMES, Client, ClientEvent, Reminder, now

PAGE = 20
MAX_REMIND_DAYS = 90
NOTE_LIMIT = 1000


class CrmError(Exception):
    pass


async def owned(s, client_id: int, user_id: int) -> Client | None:
    """У каждого пользователя своя CRM: чужих клиентов не видно."""
    c = await s.get(Client, client_id)
    return c if c is not None and c.user_id == user_id else None


async def counts(s, user_id: int) -> dict[str, int]:
    return dict((await s.execute(
        select(Client.stage, func.count()).where(Client.user_id == user_id).group_by(Client.stage))).all())


async def list_clients(s, user_id: int, stage: str = "all", query: str = "", offset: int = 0, limit: int = PAGE) -> tuple[list[Client], bool]:
    q = select(Client).where(Client.user_id == user_id).order_by(Client.updated_at.desc(), Client.id.desc())
    if stage != "all":
        q = q.where(Client.stage == stage)
    if query:
        like = f"%{query.lstrip('@')}%"
        q = q.where(or_(Client.name.ilike(like), Client.username.ilike(like), Client.phone.ilike(like)))
    rows = (await s.scalars(q.offset(offset).limit(limit + 1))).all()
    return list(rows[:limit]), len(rows) > limit


async def create_client(s, user_id: int, name: str = "", username: str = "", phone: str = "", source: str = "добавлен вручную") -> Client:
    name, username, phone = name.strip()[:255], username.strip().lstrip("@")[:64], phone.strip()[:32]
    if not (name or username or phone):
        raise CrmError("Укажите имя, @username или телефон")
    c = Client(user_id=user_id, name=name, username=username, phone=phone, source=source)
    s.add(c)
    await s.flush()
    s.add(ClientEvent(client_id=c.id, text="Добавлен вручную"))
    await s.commit()
    return c


async def set_stage(s, c: Client, stage: str) -> None:
    if stage not in STAGE_NAMES:
        raise CrmError("Неизвестный этап")
    if stage == c.stage:
        return
    c.stage = stage
    s.add(ClientEvent(client_id=c.id, text=f"Этап → {STAGE_NAMES[stage]}"))
    if stage == "thinking":  # «дожать» через 2 дня, если клиент думает
        s.add(Reminder(client_id=c.id, due_at=now() + timedelta(days=2), text="Клиент думает — дожать"))
    await s.commit()


async def add_reminder(s, c: Client, days: int) -> None:
    if not 1 <= days <= MAX_REMIND_DAYS:
        raise CrmError(f"Напоминание: от 1 до {MAX_REMIND_DAYS} дней")
    s.add(Reminder(client_id=c.id, due_at=now() + timedelta(days=days)))
    s.add(ClientEvent(client_id=c.id, text=f"Напоминание через {days} дн."))
    await s.commit()


async def update_field(s, c: Client, field: str, value: str) -> None:
    """Заметка (дописывается), бюджет (число), телефон."""
    value = value.strip()
    if field == "note":
        if not value:
            raise CrmError("Пустая заметка")
        if len(value) > NOTE_LIMIT:
            raise CrmError(f"Заметка: до {NOTE_LIMIT} символов")
        c.notes = (c.notes + "\n" if c.notes else "") + value
        s.add(ClientEvent(client_id=c.id, text=f"Заметка: {value[:200]}"))
    elif field == "budget":
        digits = "".join(ch for ch in value if ch.isdigit())
        if not digits:
            raise CrmError("Бюджет: нужно число")
        c.budget = int(digits[:9])
    elif field == "phone":
        c.phone = value[:32]
    else:
        raise CrmError("Неизвестное поле")
    await s.commit()


async def events(s, client_id: int, limit: int = 10) -> list[ClientEvent]:
    return list((await s.scalars(
        select(ClientEvent).where(ClientEvent.client_id == client_id).order_by(ClientEvent.id.desc()).limit(limit))).all())


async def next_reminder(s, client_id: int) -> Reminder | None:
    return await s.scalar(select(Reminder).where(Reminder.client_id == client_id, ~Reminder.sent).order_by(Reminder.due_at))
