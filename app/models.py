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
    can_post: Mapped[bool] = mapped_column(Boolean, default=True)  # аккаунту разрешено публиковать заказанную рекламу
    tg_user_id: Mapped[int] = mapped_column(BigInteger, default=0, index=True)  # его Telegram ID: свои посты не считаем заявками


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
    city: Mapped[str] = mapped_column(String(64), default="")  # город группы, если известен
    # group — чат, куда вступает разведчик; channel — открытый канал, читаем через t.me/s/<канал>
    kind: Mapped[str] = mapped_column(String(8), default="group")
    # группа, добавленная пользователем: draft -> pending -> approved/joined/... | rejected
    submitted_by: Mapped[int] = mapped_column(BigInteger, default=0, index=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id"), nullable=True)
    catalog: Mapped[bool] = mapped_column(Boolean, default=False, index=True)  # показана в каталоге
    last_post_id: Mapped[int] = mapped_column(Integer, default=0)  # канал: последний прочитанный пост
    bonus_rewarded: Mapped[int] = mapped_column(Integer, default=0)  # заявок уже обменяно на бонус автору
    ad_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # реклама «Рекомендуем» до
    ad_spent: Mapped[int] = mapped_column(Integer, default=0)  # заявок потрачено на рекламу
    ad_pending: Mapped[bool] = mapped_column(Boolean, default=False)  # заказ рекламы ждёт подтверждения
    license_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # лицензия бота модерации до
    license_off: Mapped[bool] = mapped_column(Boolean, default=False)  # владелец отключил лицензию вручную
    license_rewarded: Mapped[int] = mapped_column(Integer, default=0)  # заявок уже учтено в лицензии
    ads_allowed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)  # админ разрешил публиковать рекламу разведчиком
    ad_price: Mapped[int] = mapped_column(Integer, default=0)  # цена поста именно здесь, ₽ (0 — общая adpost_price)
    ad_account_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # кто публикует (для каналов — аккаунт-админ канала)
    ad_hours: Mapped[str] = mapped_column(String(16), default="")  # «9-21» — публиковать только в эти часы (местное время)
    ad_note: Mapped[str] = mapped_column(String(255), default="")  # условия группы для рекламы (заметка админа)
    no_media: Mapped[bool] = mapped_column(Boolean, default=False)  # картинки в группе запрещены — только текстовая реклама
    ad_check: Mapped[str] = mapped_column(String(160), default="")  # итог «🔎 Проверить группу»: можно ли писать, что запрещено
    ad_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ad_closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # с какого момента группа закрыта для сообщений
    ad_rules: Mapped[str] = mapped_column(Text, default="")  # что нашли при проверке: правила (описание, закреп), боты, админы
    ad_agreed: Mapped[bool] = mapped_column(Boolean, default=False)  # владелец договорился с админом группы — правила не мешают
    ad_ai_channels: Mapped[str] = mapped_column(String(160), default="")  # каналы, на которые, по мнению ИИ, надо подписаться
    join_attempts: Mapped[int] = mapped_column(Integer, default=0)  # сколько раз разведчик пытался вступить (заявка не одобрена)
    join_retry_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # когда повторить заявку на вступление
    no_leads: Mapped[bool] = mapped_column(Boolean, default=False)  # заявки отсюда не брать (реклама/флуд), services/junk.py
    skip_reason: Mapped[str] = mapped_column(String(120), default="")  # почему разведчик не вступил (видно в «🚫 Пропущены»)


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
    section: Mapped[str] = mapped_column(String(48), default="")  # раздел в списке ниш («💻 Онлайн и IT»); пусто — «📌 Другое»
    # ниша, которую предложил пользователь: ждёт проверки администратором (пока pending — никому не показывается)
    proposed_by: Mapped[int] = mapped_column(BigInteger, default=0)
    pending: Mapped[bool] = mapped_column(Boolean, default=False)
    proposed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


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


class SpamSample(Base):
    """Образец спам-текста: похожие сообщения (от любого аккаунта) больше не становятся заявками.

    Появляется, когда админ (или N голосов) отмечает заявку «спам»; список и удаление — 🛡 Чёрный список → 🧩 Спам-тексты."""

    __tablename__ = "spam_samples"
    id: Mapped[int] = mapped_column(primary_key=True)
    text: Mapped[str] = mapped_column(Text)
    lead_id: Mapped[int] = mapped_column(Integer, default=0, index=True)
    added_by: Mapped[int] = mapped_column(BigInteger, default=0)
    hits: Mapped[int] = mapped_column(Integer, default=0)  # сколько похожих сообщений уже отсеяно
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class FilteredMessage(Base):
    """Сообщение, которое похоже на заявку по словам, но отсеяно как реклама услуг (исполнитель, а не заказчик).

    Хранится 3 дня: админ видит их в 🛡 Чёрный список → «🙅 Отсеянные» и может вернуть кнопкой «↩️ Это заявка»."""

    __tablename__ = "filtered_messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    text: Mapped[str] = mapped_column(Text, default="")
    author_id: Mapped[int] = mapped_column(BigInteger, default=0)
    username: Mapped[str] = mapped_column(String(64), default="")
    first_name: Mapped[str] = mapped_column(String(128), default="")
    group_tg_id: Mapped[int] = mapped_column(BigInteger, default=0)
    group_title: Mapped[str] = mapped_column(String(255), default="")
    message_id: Mapped[int] = mapped_column(Integer, default=0)
    link: Mapped[str] = mapped_column(String(255), default="")
    reason: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)


class LabeledText(Base):
    """Пример, на котором учится бесплатный фильтр «заказчик или исполнитель»: админ сам отметил сообщение.

    kind: offer — «🙅 Исполнитель» под заявкой; request — «↩️ Это заявка» в отсеянных. Похожие тексты дальше решаются так же."""

    __tablename__ = "labeled_texts"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(8), index=True)
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)


class ErrorLog(Base):
    """Журнал сбоев (то же, что приходит тревогой 🚨) — для «🩺 Отчёт о проблемах»."""

    __tablename__ = "error_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(64), index=True)
    text: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)


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
    city: Mapped[str] = mapped_column(String(64), default="")  # город из текста или группы
    region: Mapped[str] = mapped_column(String(64), default="")
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
    # чей это клиент: у каждого пользователя своя CRM
    user_id: Mapped[int] = mapped_column(BigInteger, default=0, index=True)
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


# ---------- пользователи (многопользовательский режим) ----------
PLANS = {"free": "🆓 Бесплатный", "full": "💎 Полный"}


class User(Base):
    __tablename__ = "users"
    tg_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    username: Mapped[str] = mapped_column(String(64), default="")
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    plan: Mapped[str] = mapped_column(String(16), default="free")
    plan_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # None — бессрочно
    city: Mapped[str] = mapped_column(String(64), default="")  # пусто — вся Россия
    only_city: Mapped[bool] = mapped_column(Boolean, default=False)  # без заявок «без города»
    quiet_hours: Mapped[str] = mapped_column(String(8), default="")
    onboarded: Mapped[bool] = mapped_column(Boolean, default=False)
    paused: Mapped[bool] = mapped_column(Boolean, default=False)
    reply_template: Mapped[str] = mapped_column(Text, default="")  # свой шаблон ответа клиенту
    prices: Mapped[str] = mapped_column(Text, default="")
    blocked: Mapped[bool] = mapped_column(Boolean, default=False)  # заблокировал бота или забанен
    referred_by: Mapped[int] = mapped_column(BigInteger, default=0, index=True)  # кто пригласил
    ref_rewarded: Mapped[bool] = mapped_column(Boolean, default=False)  # награда за приглашение выдана
    source: Mapped[str] = mapped_column(String(24), default="")  # откуда пришёл: метка из ссылки ?start=src_<метка> (сайт и т. п.)
    level: Mapped[int] = mapped_column(Integer, default=0)  # уровень автора групп (services/levels.py)
    verified: Mapped[bool] = mapped_column(Boolean, default=True)  # прошёл проверку «я не бот» (у старых — да)
    warned_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # когда предупредили об удалении
    renew_method: Mapped[str] = mapped_column(String(64), default="")  # сохранённый способ оплаты ЮKassa (автопродление)
    renew_card: Mapped[str] = mapped_column(String(64), default="")  # как показываем карту человеку: «Mir •••• 1234» (номер не храним)
    renew_days: Mapped[int] = mapped_column(Integer, default=0)  # на сколько дней продлевать
    renew_for: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # plan_until, для которого уже была попытка
    expiry_notice: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # plan_until, о котором уже напомнили
    winback_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # когда написать «вернуть 💎» (через 3 дня после конца), один раз
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=now)


class UserCategory(Base):
    """Ниши пользователя (корневые категории)."""

    __tablename__ = "user_categories"
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id"), primary_key=True)


class UserNegative(Base):
    """Личные минус-слова: такие заявки пользователю не приходят."""

    __tablename__ = "user_negatives"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    word: Mapped[str] = mapped_column(String(128))


class Delivery(Base):
    """Заявка, отданная конкретному пользователю. Статус обработки — у каждого свой."""

    __tablename__ = "deliveries"
    id: Mapped[int] = mapped_column(primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id"), index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    # new / postponed / taken / rejected / spam
    status: Mapped[str] = mapped_column(String(12), default="new", index=True)
    deliver_after: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)
    sent: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    from_user: Mapped[int] = mapped_column(BigInteger, default=0)  # кто передал заявку коллеге


class Payment(Base):
    """Оплата тарифа через Telegram Payments (сумма в копейках)."""

    __tablename__ = "payments"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    days: Mapped[int] = mapped_column(Integer)
    amount: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(8), default="RUB")
    charge_id: Mapped[str] = mapped_column(String(128), unique=True)  # id платежа Telegram — защита от повторов
    provider_id: Mapped[str] = mapped_column(String(128), default="")  # id платежа в ЮKassa — нужен для возврата
    refunded: Mapped[bool] = mapped_column(Boolean, default=False)
    refunded_amount: Mapped[int] = mapped_column(Integer, default=0)  # уже возвращено, копейки (частичный возврат)
    refunded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class PendingLink(Base):
    """Платёж по ссылке ЮKassa (СБП/карта в браузере): ждём, пока клиент заплатит."""

    __tablename__ = "pending_links"
    id: Mapped[int] = mapped_column(primary_key=True)
    yk_id: Mapped[str] = mapped_column(String(64), unique=True)  # id платежа в ЮKassa
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    days: Mapped[int] = mapped_column(Integer)
    amount: Mapped[int] = mapped_column(Integer)  # в копейках
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)
    done: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    auto: Mapped[bool] = mapped_column(Boolean, default=False)  # человек согласился на автопродление
    order_id: Mapped[int] = mapped_column(Integer, default=0)  # не 0 — это оплата заказа рекламы, а не тарифа


class AiCall(Base):
    """Один запрос к ИИ: для суточных лимитов и учёта токенов."""

    __tablename__ = "ai_calls"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    model: Mapped[str] = mapped_column(String(48), default="")
    purpose: Mapped[str] = mapped_column(String(24), default="")  # draft / leads / rules / bots / ad_text / groups / error
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)


class SpamVote(Base):
    """Голоса «это спам» от пользователей; несколько голосов — автор в общий чёрный список."""

    __tablename__ = "spam_votes"
    author_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class OfferVote(Base):
    """Голоса «🙅 Исполнитель» (заявка — реклама услуг, а не заказчик). Админ или N голосов — фильтр учится."""

    __tablename__ = "offer_votes"
    lead_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class AdOrder(Base):
    """Заказ рекламы: пользователь шлёт текст и выбирает группы, разведчики публикуют.

    draft -> pending (ждёт проверки админом) -> approved (ждёт оплаты; бесплатно — сразу running) -> running -> done
    | rejected | canceled
    """

    __tablename__ = "ad_orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    text: Mapped[str] = mapped_column(Text, default="")
    group_ids: Mapped[str] = mapped_column(Text, default="")  # выбранные группы (id через запятую), пока заказ черновик
    price: Mapped[int] = mapped_column(Integer, default=0)  # итого, ₽ (0 — бесплатно)
    status: Mapped[str] = mapped_column(String(16), default="draft", index=True)
    yk_id: Mapped[str] = mapped_column(String(64), default="")  # платёж ЮKassa
    ai_check: Mapped[str] = mapped_column(String(300), default="")  # что сказал ИИ-помощник о тексте рекламы (пусто — не проверял)
    photo: Mapped[str] = mapped_column(String(255), default="")  # картинка к рекламе: файл в data/ad_media ("" — без неё)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    reminded: Mapped[bool] = mapped_column(Boolean, default=False)  # напомнили об оплате
    reason: Mapped[str] = mapped_column(String(300), default="")  # причина отказа/отмены (её видит заказчик)
    # маркировка рекламы (ФЗ «О рекламе»): токен из ОРД и данные рекламодателя — дописываются под пост
    erid: Mapped[str] = mapped_column(String(64), default="")
    advertiser: Mapped[str] = mapped_column(String(120), default="")


class AdPost(Base):
    """Одна публикация заказа в одной группе."""

    __tablename__ = "ad_posts"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("ad_orders.id"), index=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id"), index=True)
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)  # queued / sent / failed / canceled
    scheduled_at: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    message_id: Mapped[int] = mapped_column(Integer, default=0)
    link: Mapped[str] = mapped_column(String(255), default="")
    error: Mapped[str] = mapped_column(String(255), default="")
    first_try_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # группа была закрыта — ждём до суток
    price: Mapped[int] = mapped_column(Integer, default=0)  # доля цены заказа за эту группу, ₽ (для частичного возврата)
    checked: Mapped[bool] = mapped_column(Boolean, default=False)  # все проверки «пост на месте» пройдены
    checks: Mapped[int] = mapped_column(Integer, default=0)  # сколько проверок уже было (15 мин, 3 ч, сутки)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # когда последний раз видели пост на месте
    fulfilled: Mapped[bool] = mapped_column(Boolean, default=False)  # уже выполняли условия группы и пробовали снова (один раз)
    settled: Mapped[bool] = mapped_column(Boolean, default=False)  # пост не вышел и вопрос денег за него закрыт (вернули / решили не возвращать)
