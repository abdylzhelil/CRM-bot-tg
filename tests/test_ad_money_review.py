from unittest.mock import AsyncMock, MagicMock

from aiogram.types import CallbackQuery
from sqlalchemy import select

from app.bot.handlers import adposts as handler
from app.models import AdOrder, AdPost, Payment
from app.services import adposts, payments
from tests.conftest import make_user
from tests.test_adposts import setup_group, make_order


async def test_unsettled_money_is_visible_without_refund_or_repricing(maker, tgbot, monkeypatch):
    await setup_group(maker, price=300)
    await make_user(maker, 70)
    oid = await make_order(maker)
    provider = AsyncMock(side_effect=AssertionError("money review must not call payment provider"))
    monkeypatch.setattr(payments, "_api", provider)
    monkeypatch.setattr(payments, "_yookassa_refund", provider)
    async with maker() as s:
        order = await s.get(AdOrder, oid)
        await adposts.approve(s, order)
        _, pay = await adposts.mark_paid(s, oid, "synthetic", 30000)
        post = await s.scalar(select(AdPost))
        order.status, post.status = "done", "failed"
        pay.refunded_amount = 10000
        await s.commit()
        rows = await adposts.money_review(s)
        assert [(o.id, amount) for o, amount in rows] == [(oid, 200)]
        assert not s.dirty and not post.settled and pay.refunded_amount == 10000
    cb = MagicMock(spec=CallbackQuery)
    cb.message = MagicMock()
    cb.message.edit_text = AsyncMock()
    cb.answer = AsyncMock()
    await handler.money_review(cb)
    assert "200 ₽" in cb.message.edit_text.call_args.args[0]
    assert any(button.callback_data == f"apa:o:{oid}" for row in cb.message.edit_text.call_args.kwargs["reply_markup"].inline_keyboard for button in row)
    async with maker() as s:
        post = await s.scalar(select(AdPost))
        post.price = 0
        await s.commit()
        rows = await adposts.money_review(s)
        assert len(rows) == 1 and rows[0][1] is None
        assert post.price == 0 and not s.dirty
        post.settled = True
        await s.commit()
        assert await adposts.money_review(s) == []
    assert provider.await_count == 0
