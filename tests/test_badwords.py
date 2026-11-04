"""«Не те заявки» (25.10): слово, по которому заявка пришла, и выключение слов, на которые жмут «Не моё»."""
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy import select

from app.models import Delivery, Keyword, Lead
from app.services import badwords, intake
from tests.conftest import make_user
from tests.test_multiuser import msg


async def test_matched_words_and_toggle(maker, tgbot):
    from app.bot.handlers import keywords

    await make_user(maker, 50, plan="full")
    ids = []
    for i in range(3):
        lead = await intake.handle(msg(f"Ищу кто сделает лендинг для пекарни номер {i}"))
        ids.append(lead.id)
    async with maker() as s:
        lead = await s.get(Lead, ids[0])
        assert "лендинг" in lead.matched
        for d in (await s.scalars(select(Delivery).where(Delivery.lead_id.in_(ids)))).all():
            d.status = "rejected"
        await s.commit()
        rows = await badwords.suspects(s)
    row = next(r for r in rows if "лендинг" in r["phrase"])
    assert row["rejected"] == 3 and row["keyword_id"]
    cb = MagicMock()
    cb.data, cb.answer = f"kw:tg:{row['keyword_id']}", AsyncMock()
    cb.message.edit_text = AsyncMock()
    await keywords.toggle_word(cb)
    async with maker() as s:
        assert (await s.get(Keyword, row["keyword_id"])).kind.startswith("x_")
    again = await intake.handle(msg("Ищу кто сделает лендинг для кофейни"))
    assert again is None or "лендинг" not in again.matched  # слово выключено — по нему заявка не приходит
    cb.data = f"kw:tg:{row['keyword_id']}"
    await keywords.toggle_word(cb)  # включить обратно
    async with maker() as s:
        assert not (await s.get(Keyword, row["keyword_id"])).kind.startswith("x_")
