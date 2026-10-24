"""Заказчик или исполнитель: реклама услуг не приходит как заявка."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy import func, select

from app import config
from app.models import AiCall, FilteredMessage, Lead
from app.services import intake, offers, settings
from app.services.antispam import AuthorInfo
from app.services.pipeline import Incoming
from tests.conftest import ADMIN_ID, make_user

OFFER = ("🚀 Нужен сайт именно под ваш бизнес? Сделаю за 3 дня, от 14000 ₽\n\nЧем занимаюсь:\n🌐 сайты и лендинги\n"
         "🤖 Telegram-боты\n📊 CRM под ваши процессы\n\n📂 Примеры работ скину в лс")
REQUEST = "Ищу кто сделает сайт для салона красоты, бюджет до 40 тысяч, подскажите хорошего разработчика"


def msg(text, author=9100):
    return Incoming(text=text, author=AuthorInfo(user_id=author, username="u", first_name="Иван"),
                    group_tg_id=-100900, group_title="Бизнес | Предприниматели РФ", message_id=author)


def test_rules():
    assert offers.rule_verdict(OFFER) == "offer"
    assert offers.rule_verdict(REQUEST) == "request"
    assert offers.rule_verdict("Нужен лендинг для кофейни, кто возьмётся?") == "request"
    assert offers.rule_verdict("Делаю сайты. Кто знает, где найти клиентов?") == "unsure"


async def test_offer_filtered_and_restorable(maker, tgbot):
    await make_user(maker, 60, plan="full")
    assert await intake.handle(msg(OFFER)) is None
    assert await intake.handle(msg(REQUEST, 9101)) is not None
    async with maker() as s:
        f = await s.scalar(select(FilteredMessage))
        assert f is not None and "реклама услуг" in f.reason
    # админ: «↩️ Это заявка» — сообщение становится заявкой и раздаётся
    from app.bot.handlers import blacklist

    c = MagicMock()
    c.data, c.from_user.id = f"bl:fok:{f.id}", ADMIN_ID
    c.answer, c.message.edit_text, c.message.answer = AsyncMock(), AsyncMock(), AsyncMock()
    await blacklist.filtered_restore(c)
    async with maker() as s:
        assert await s.scalar(select(func.count()).select_from(Lead)) == 2
        assert await s.get(FilteredMessage, f.id) is None


async def test_ai_decides_unsure(maker, tgbot, monkeypatch):
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "k")
    client = MagicMock()
    client.messages.create = AsyncMock(return_value=SimpleNamespace(
        stop_reason="end_turn", content=[SimpleNamespace(type="text", text='{"kind": "ИСПОЛНИТЕЛЬ"}')],
        usage=SimpleNamespace(input_tokens=200, output_tokens=3)))
    async with maker() as s:
        assert await offers.verdict(s, "Делаю сайты. Кто знает, где найти клиентов?", client) == ("offer", "ИИ")
        await s.commit()
        assert await s.scalar(select(func.count()).select_from(AiCall)) == 1
        kw = client.messages.create.call_args.kwargs
        assert kw["model"] == "claude-haiku-5-5" and "Иван" not in str(kw["messages"])
        await settings.put(s, "ai_classify", "0")  # выключили — ИИ не спрашиваем, сообщение остаётся заявкой
        assert await offers.verdict(s, "Делаю сайты. Кто знает, где найти клиентов?", client) == ("request", "правила")
    assert client.messages.create.await_count == 1


async def test_ai_error_keeps_lead(maker, monkeypatch):
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "k")
    client = MagicMock()
    client.messages.create = AsyncMock(side_effect=RuntimeError("down"))
    async with maker() as s:
        assert (await offers.verdict(s, "Делаю сайты. Кто знает, где найти клиентов?", client))[0] == "request"


REPORT_OFFERS = [  # отчёт владельца 18.10: это пропустил фильтр
    '#помогу #таргетолог 2 млн чистыми за месяц. Меня зовут Алена. Я специалист по рекламе во ВКонтакте. Работаю 4 года',
    "Разблокировка аккаунта на авито Пишите помогу Есть отзывы Есть опыт Есть гарантия Есть оплата после разблокировки",
    "Всем привет, ищу кому актуальны будут мои услуги, в профиле есть сайт с моими работами.",
    "Привет! Если нужно, могу сделать простого Telegram-бота: принимает заявки. Могу показать демо",
    "Добрый день! Ищу партнерскую сеть для интеграции своего аутсорсингового digital-маркетинг агентства полного цикла",
]
STILL_REQUESTS = [
    "Ищу таргетолога для салона, бюджет 30 тысяч", "Подскажите юриста по разводу, есть опыт у кого?",
    "Нужен мастер, кто может сделать ремонт ванной? Оплата после работы", "Нужен грузчик на 2 часа завтра, оплата 1500",
    "Ищу специалиста по авито, кто может разблокировать аккаунт",
]


def test_report_examples():
    for t in REPORT_OFFERS:
        assert offers.rule_verdict(t) == "offer", t
    vacancy = "Нужны продавцы кассиры и пекарь, официальное оформление, график 2/2 зп 3500р чистыми за смену"
    assert offers.rule_verdict(vacancy) == "other"
    for t in STILL_REQUESTS:
        assert offers.rule_verdict(t) == "request", t


async def test_double_start_does_not_crash(maker, monkeypatch):
    """Два нажатия /start подряд: второй запрос не падает с UNIQUE users.tg_id."""
    import asyncio
    from types import SimpleNamespace

    from app.bot import captcha
    from app.bot.access import UserMiddleware

    async def gate(event, handler, data, user):  # новичок сначала видит проверку «я не бот» — здесь пропускаем её
        return await handler(event, data)
    monkeypatch.setattr(captcha, "gate", gate)
    mw = UserMiddleware()
    tg = SimpleNamespace(id=555001, full_name="Новый", username="", is_bot=False)
    seen = []

    async def handler(event, data):
        seen.append(data["user"].tg_id)

    await asyncio.gather(*(mw(handler, object(), {"event_from_user": tg, "event_chat": None}) for _ in range(3)))
    assert seen == [555001] * 3


async def test_ai_checks_every_lead_and_niche(maker, monkeypatch):
    """Владелец 24.10: «с заявками тоже нужно с ИИ, не то часто приходят» — ИИ смотрит и «ясные» по правилам заявки."""
    from app import config
    from app.services import llm, settings

    monkeypatch.setattr(config, "GIGACHAT_AUTH_KEY", "g")
    seen = []

    async def fake_complete(provider, system, text, max_tokens=400, json_mode=False):
        seen.append(text)
        return '{"kind": "НЕ_ПО_НИШЕ"}' if "карту" in text else '{"kind": "ЗАКАЗЧИК"}'
    monkeypatch.setattr(llm, "complete", fake_complete)
    async with maker() as s:
        assert await offers.verdict(s, "Ищу кто сделает перевод на карту срочно", niche="Переводчики") == ("other", "ИИ: не по нише")
        assert "Ниша: Переводчики" in seen[-1]
        assert await offers.verdict(s, "Ищу сантехника, течёт кран") == ("request", "ИИ")
        await settings.put(s, "ai_check_leads", "0")  # выключено — «ясные» заявки ИИ не тратят
        n = len(seen)
        assert await offers.verdict(s, "Ищу сантехника, течёт кран") == ("request", "правила")
        assert len(seen) == n


def test_report_24_10():
    """Отчёт 24.10: самоописание исполнителя, «помогаю собственникам», подработка «N на руки»; просьбы клиентов не трогаем."""
    assert offers.rule_verdict("Здравствуйте! Я дизайнер карточек товаров для маркетплейсов. Создаю аккуратные карточки в figma. "
                               "Работаю удалённо, внимательно отношусь к пожеланиям заказчика.") == "offer"
    assert offers.rule_verdict("Не устраивают лиды или продажи? Не спешите менять рекламу. "
                               "Помогаю собственникам и руководителям разобраться") == "offer"
    assert offers.rule_verdict("Привет! Кто сможет привести в порядок небольшое помещение? Простые действия, всё "
                               "необходимое есть. Займёт примерно 3-4 часа, 7000 на руки.") == "other"
    for t in ("Ищу мастера, нужно поклеить обои, займёт примерно день, бюджет 5000", "Кто создаёт сайты? Нужен лендинг",
              "Нужен сантехник срочно, оплата сразу после работы"):
        assert offers.rule_verdict(t) == "request", t


def test_report_25_10_vacancies():
    """25.10, скриншот рейтинга: вакансии «за выход / по ТК РФ / работа из дома» и «кому надо — пишите» не заявки."""
    for t in ("Специалист по оформлению воздушных шаров — 4200 за выход. С 9:00 до 15:00, 5/2. Всему необходимому обучат. Пиши в лс",
              "Работа из дома: менеджер чата, доход от 60 000 ₽. Я набираю команду",
              "На склад мебели требуется упаковщик. Рассматриваем кандидатов без опыта. Зарплата от 99 000 ₽, оформление по ТК РФ"):
        assert offers.rule_verdict(t) == "other", t
    assert offers.rule_verdict("Здравствуйте 👋 Каму надо инфографика пишите в личные сообщения") == "offer"
    for t in ("Ищу инфографика, срочно !", "Нужен репетитор по математике 5/6 класс, кто может?",
              "Нужен мастер с 10:00 до 12:00 завтра, кто свободен?"):
        assert offers.rule_verdict(t) == "request", t
