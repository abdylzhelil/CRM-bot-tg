"""🤝 Предложить бота админам: готовый текст, чат с уже вставленным текстом, статусы. Пишет владелец сам."""
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import unquote

from app.models import Group, now
from app.services import botoffer, settings


async def test_offer_flow(maker):
    from app.bot.handlers import groups as groups_h

    async with maker() as s:
        await settings.put(s, "mod_bot_username", "my_moder_bot")
        s.add_all([Group(tg_id=-1, title="Барахолка", status="joined", members=900, admins="boss,helper"),
                   Group(tg_id=-2, title="С капчей", status="joined", members=100, rules_contact="ads_admin",
                         captcha_since=now()),
                   Group(tg_id=-3, title="Без админов", status="joined", members=5000)])
        await s.commit()
        todo = await botoffer.todo(s)
        assert [g.title for g in todo] == ["С капчей", "Барахолка"]  # капча первой, без админа — нет
        msg = await botoffer.text(s, todo[0])
    assert "@my_moder_bot" in msg and "капча" in msg and len(msg) <= 256
    url = botoffer.link(botoffer.contact(todo[0]), msg)
    assert url.startswith("https://t.me/ads_admin?text=") and unquote(url.split("text=")[1]) == msg
    assert botoffer.contact(todo[1]) == "boss"

    cb = MagicMock()
    cb.answer, cb.message.edit_text, cb.message.answer = AsyncMock(), AsyncMock(), AsyncMock()
    cb.data = f"gr:bos:{todo[0].id}:sent"
    await groups_h.offer_set(cb)
    cb.data = f"gr:bos:{todo[0].id}:ads"
    await groups_h.offer_set(cb)
    async with maker() as s:
        g = await s.get(Group, todo[0].id)
        assert g.offer_status == "ads" and g.ad_agreed  # договорились — правила рекламе не мешают
        assert (await botoffer.counts(s))[""] == 1
    cb.data = f"gr:bos:{todo[0].id}:"  # вернуть
    await groups_h.offer_set(cb)
    async with maker() as s:
        assert (await s.get(Group, todo[0].id)).offer_status == ""


async def test_menu_shows_cards(maker):
    from app.bot.handlers import groups as groups_h

    async with maker() as s:
        s.add(Group(tg_id=-5, title="Город", status="joined", members=300, admins="boss"))
        await s.commit()
    cb = MagicMock()
    cb.data, cb.answer, cb.message.answer = "gr:bo", AsyncMock(), AsyncMock()
    await groups_h.offer_menu(cb)
    card = cb.message.answer.call_args
    buttons = [b for row in card.kwargs["reply_markup"].inline_keyboard for b in row]
    assert any((b.url or "").startswith("https://t.me/boss?text=") for b in buttons)
