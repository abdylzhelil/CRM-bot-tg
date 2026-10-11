from datetime import datetime, timezone

from sqlalchemy import BigInteger, Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.ext.asyncio import AsyncAttrs
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(AsyncAttrs, DeclarativeBase):
    pass


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class ScoutAccount(Base):
    """Аккаунт-разведчик. Для N аккаунтов — N строк, у каждого свой прокси и лимиты."""

    __tablename__ = "scout_accounts"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    api_id: Mapped[int] = mapped_column(Integer)
    api_hash: Mapped[str] = mapped_column(String(64))
    phone: Mapped[str] = mapped_column(String(32), default="")
    session: Mapped[str] = mapped_column(Text, default="")  # StringSession
    proxy: Mapped[str] = mapped_column(String(255), default="")  # socks5://user:pass@host:port
    daily_join_limit: Mapped[int] = mapped_column(Integer, default=10)
    joined_today: Mapped[int] = mapped_column(Integer, default=0)
    joined_date: Mapped[str] = mapped_column(String(10), default="")
    max_groups: Mapped[int] = mapped_column(Integer, default=500)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(32), default="new")  # new/ok/limited/dead


class Group(Base):
    __tablename__ = "groups"
    id: Mapped[int] = mapped_column(primary_key=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    username: Mapped[str] = mapped_column(String(64), default="")
    title: Mapped[str] = mapped_column(String(255), default="")
    members: Mapped[int] = mapped_column(Integer, default=0)
    msgs_per_day: Mapped[float] = mapped_column(Float, default=0)
    lang: Mapped[str] = mapped_column(String(8), default="")
    # candidate -> approved -> joined | skipped | left
    status: Mapped[str] = mapped_column(String(16), default="candidate", index=True)
    account_id: Mapped[int | None] = mapped_column(ForeignKey("scout_accounts.id"), nullable=True)
    found_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    joined_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    leads_count: Mapped[int] = mapped_column(Integer, default=0)
    last_lead_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class FeedChat(Base):
    """Группа бота модерации. Сообщения из неё идут в CRM, только если enabled (включает владелец)."""

    __tablename__ = "feed_chats"
    tg_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    title: Mapped[str] = mapped_column(String(255), default="")
    username: Mapped[str] = mapped_column(String(64), default="")
    own: Mapped[bool] = mapped_column(Boolean, default=False)  # группа владельца CRM, а не клиента
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    seen_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class SearchQuery(Base):
    __tablename__ = "search_queries"
    id: Mapped[int] = mapped_column(primary_key=True)
    text: Mapped[str] = mapped_column(String(128), unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class Category(Base):
    """Дерево категория -> подкатегория (parent_id = NULL у корневых)."""

    __tablename__ = "categories"
    id: Mapped[int] = mapped_column(primary_key=True)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id"), nullable=True)
    name: Mapped[str] = mapped_column(String(128))
    emoji: Mapped[str] = mapped_column(String(8), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    city: Mapped[str] = mapped_column(String(64), default="")  # фильтр по городу (напр. Махачкала)
    reply_template: Mapped[str] = mapped_column(Text, default="")
    prices: Mapped[str] = mapped_column(Text, default="")


class Keyword(Base):
    """kind: intent — слова-намерения (category_id NULL),
    category — слова категории, strong — сильные фразы, negative — минус-слова.
    category_id NULL у negative — глобальные минус-слова."""

    __tablename__ = "keywords"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), index=True)
    word: Mapped[str] = mapped_column(String(128))
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id"), nullable=True)


class BlacklistEntry(Base):
    """status: spam — чёрный список (общий для всех групп), client — белый список."""

    __tablename__ = "blacklist"
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(8), default="spam")
    reason: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class SeenMessage(Base):
    """Хеши текстов — для поиска одинаковых сообщений в разных группах и антидублей."""

    __tablename__ = "seen_messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    text_hash: Mapped[str] = mapped_column(String(40), index=True)
    author_id: Mapped[int] = mapped_column(BigInteger, index=True)
    group_id: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)


class Lead(Base):
    __tablename__ = "leads"
    id: Mapped[int] = mapped_column(primary_key=True)
    author_id: Mapped[int] = mapped_column(BigInteger, index=True)
    username: Mapped[str] = mapped_column(String(64), default="")
    name: Mapped[str] = mapped_column(String(255), default="")
    group_id: Mapped[int | None] = mapped_column(ForeignKey("groups.id"), nullable=True)
    group_title: Mapped[str] = mapped_column(String(255), default="")
    message_id: Mapped[int] = mapped_column(BigInteger, default=0)
    link: Mapped[str] = mapped_column(String(255), default="")
    text: Mapped[str] = mapped_column(Text)
    text_hash: Mapped[str] = mapped_column(String(40), index=True)
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id"), nullable=True)
    spam_score: Mapped[int] = mapped_column(Integer, default=0)
    suspicious: Mapped[bool] = mapped_column(Boolean, default=False)
    # new / postponed / taken / rejected / spam
    status: Mapped[str] = mapped_column(String(12), default="new", index=True)
    notified: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)


STAGES = [
    ("new", "🆕 Новый"),
    ("talk", "💬 В переговорах"),
    ("thinking", "🤔 Думает"),
    ("payment", "💳 Оплата"),
    ("done", "✅ Выполнено"),
    ("lost", "❌ Отказ"),
]
STAGE_NAMES = dict(STAGES)


class Client(Base):
    __tablename__ = "clients"
    id: Mapped[int] = mapped_column(primary_key=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, default=0, index=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    username: Mapped[str] = mapped_column(String(64), default="")
    phone: Mapped[str] = mapped_column(String(32), default="")
    source: Mapped[str] = mapped_column(String(255), default="")
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id"), nullable=True)
    budget: Mapped[int] = mapped_column(Integer, default=0)
    stage: Mapped[str] = mapped_column(String(12), default="new", index=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class ClientEvent(Base):
    __tablename__ = "client_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"), index=True)
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Reminder(Base):
    __tablename__ = "reminders"
    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"), index=True)
    due_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    text: Mapped[str] = mapped_column(String(255), default="Дожать клиента")
    sent: Mapped[bool] = mapped_column(Boolean, default=False)
