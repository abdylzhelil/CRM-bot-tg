from unittest.mock import AsyncMock, MagicMock

from app.bot.handlers import settings as sh
from app.services import settings


def test_every_setting_is_in_exactly_one_section():
    keys = [k for _, _, ks in settings.SECTIONS for k in ks]
    assert sorted(keys) == sorted(settings.LABELS) and len(keys) == len(set(keys))
    assert all(k in settings.SHORT for k in keys)  # у каждого пункта есть короткая подпись для кнопки


def cb(data):
    c = MagicMock()
    c.data = data
    c.answer = AsyncMock()
    return c


def buttons(c):
    markup = c.answer.call_args.kwargs["reply_markup"]
    return [b.text for row in markup.inline_keyboard for b in row]


async def test_main_and_section_pages(maker):
    c = cb("st:menu")
    await sh.back(c)
    main = buttons(c)
    assert len(main) <= 14 and "💎 Тарифы и цены" in main  # раньше было ~28 кнопок в столбик
    c = cb("st:sec:0")
    await sh.section(c)
    page = buttons(c)
    assert "30 дней, ₽: 0" in page and page[-1] == "◀️ Все настройки"
