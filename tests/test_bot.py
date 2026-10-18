"""Дымовые тесты хендлеров бота (без сети): вызываем функции напрямую с моками."""
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy import select

from app.bot import notify
from app.bot.handlers import inputs, leads, stats
from app.models import BlacklistEntry, Category, Client, Delivery, Keyword, Lead, User
from tests.conftest import ADMIN_ID


def cb(data, uid=ADMIN_ID):
    c = MagicMock()
    c.data = data
    c.from_user.id = uid
    c.answer, c.message.edit_reply_markup, c.message.answer = AsyncMock(), AsyncMock(), AsyncMock()
    return c


async def admin_delivery(maker, **lead_fields):
    async with maker() as s:
        lead = Lead(text_hash="h", group_title="G", **lead_fields)
        s.add(lead)
        await s.flush()
        d = Delivery(lead_id=lead.id, user_id=ADMIN_ID, sent=True)
        s.add(d)
        await s.commit()
        return d, await s.get(User, ADMIN_ID)


async def test_take_and_spam_lead(maker):
    d, admin = await admin_delivery(maker, author_id=7, username="vasya", name="Вася", text="Ищу лендинг")
    await leads.lead_action(cb(f"lead:take:{d.id}"), admin)
    async with maker() as s:
        client = await s.scalar(select(Client).where(Client.tg_id == 7))
        assert client and client.stage == "new" and client.user_id == ADMIN_ID
    await leads.lead_action(cb(f"lead:spam:{d.id}"), admin)
    async with maker() as s:
        assert (await s.get(BlacklistEntry, 7)).status == "spam"  # админ — сразу в общий список


async def test_keyword_edit_via_input(maker):
    m = AsyncMock()
    async with maker() as s:
        await inputs.kw_add(s, m, {"kind": "intent", "cid": 0}, "хочу, надо")
        assert await s.scalar(select(Keyword).where(Keyword.word == "надо"))
        await inputs.kw_del(s, m, {"kind": "intent", "cid": 0}, "надо")
        assert await s.scalar(select(Keyword).where(Keyword.word == "надо")) is None


async def test_report_and_notify_text(maker):
    assert "Найдено заявок" in await stats.build_report()
    assert "Ваши заявки" in await stats.user_report(ADMIN_ID)
    lead = Lead(id=1, author_id=1, username="u", name="N", text="<b>x</b>", text_hash="h", group_title="G",
                link="https://t.me/g/1", city="Каспийск")
    text = notify.lead_text(lead, "💻 Сайты")
    assert "&lt;b&gt;" in text and "Новый клиент" in text and "Каспийск" in text


async def test_template_admin_falls_back_to_category_prices(maker):
    async with maker() as s:
        cat = await s.scalar(select(Category).where(Category.name == "Сайты"))
        cat.prices = "лендинг от 10 000 ₽"
        await s.commit()
    d, admin = await admin_delivery(maker, author_id=9, username="m", name="Марат", text="нужен лендинг",
                                    category_id=cat.id)
    c = cb(f"lead:tpl:{d.id}")
    await leads.lead_action(c, admin)
    sent = c.message.answer.call_args.args[0]
    assert "Марат" in sent and "10 000" in sent


async def test_client_add_manual(maker):
    m = AsyncMock()
    m.from_user.id = 77
    async with maker() as s:
        await inputs.client_add(s, m, {}, "Марат @marat_shop +79001234567")
        c = await s.scalar(select(Client).where(Client.username == "marat_shop"))
    assert c.name == "Марат" and c.phone == "+79001234567" and c.tg_id == 0 and c.user_id == 77


async def test_admin_actions_blocked_for_users(maker):
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage

    state = FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=5, user_id=5))
    await state.update_data(action="kw_add", kind="intent", cid=0)
    m = AsyncMock()
    m.text = "взлом"
    await inputs.on_input(m, state, user=User(tg_id=5, is_admin=False))
    async with maker() as s:
        assert await s.scalar(select(Keyword).where(Keyword.word == "взлом")) is None


async def test_offer_button_stays_after_reject(maker):
    """После «🙈 Не моё» админ всё ещё может нажать «🙅 Исполнитель» и научить фильтр."""
    from app.models import LabeledText

    text = "Продаю ноутбуки дешевле чем на авито, пишите в личку, доставка по городу"
    d, admin = await admin_delivery(maker, author_id=8, username="nik", name="Ник", text=text)
    c = cb(f"lead:reject:{d.id}")
    await leads.lead_action(c, admin)
    markup = c.message.edit_reply_markup.call_args.kwargs["reply_markup"]
    assert f"lead:offer:{d.id}" in [b.callback_data for row in markup.inline_keyboard for b in row]
    await leads.lead_action(cb(f"lead:offer:{d.id}"), admin)
    async with maker() as s:
        assert await s.scalar(select(LabeledText).where(LabeledText.kind == "offer"))
        d2 = await s.get(Delivery, d.id)
        lead = await s.get(Lead, d2.lead_id)
        kb2 = leads.card_keyboard(lead, d2, admin)
    assert f"lead:offer:{d.id}" in [b.callback_data for row in kb2.inline_keyboard for b in row]


async def test_template_has_copy_and_group_buttons(maker):
    """Бот сам клиенту не пишет: под ответом — «скопировать» и «ответить в группе» (заявка без @ника)."""
    d, admin = await admin_delivery(maker, author_id=8777, username=None, name="Есения", text="нужен дизайнер карточек",
                                    link="https://t.me/mp_chat/55")
    c = cb(f"lead:tpl:{d.id}")
    await leads.lead_action(c, admin)
    sent = c.message.answer.call_args.args[0]
    markup = c.message.answer.call_args.kwargs["reply_markup"]
    buttons = [b for row in markup.inline_keyboard for b in row]
    copy = next(b for b in buttons if b.copy_text)
    assert "Есения" in copy.copy_text.text
    assert any(b.url == "https://t.me/mp_chat/55" and "в группе" in b.text for b in buttons)
    assert "скрыл профиль" in sent and "Ответить в группе" in sent


def test_reply_rows_long_text_and_no_link():
    lead = Lead(author_id=1, username="m", text="x", link="")
    rows = leads.reply_rows(lead, "а" * 300)  # длиннее предела Telegram — без кнопки копирования
    flat = [b for row in rows for b in row]
    assert not any(d.startswith("copy:") for _, d in flat)
    assert flat == [("✍️ Написать в личку", "url:https://t.me/m")]
    assert "нажмите на текст" in leads.reply_hint(lead, "а" * 300)


def test_lead_card_has_group_button():
    lead = Lead(author_id=1, username=None, text="x", link="https://t.me/c/123/9")
    urls = [b.url for row in notify.lead_keyboard(lead, 5).inline_keyboard for b in row if b.url]
    assert "https://t.me/c/123/9" in urls
    assert "состоите" in leads.reply_hint(lead, "привет")
