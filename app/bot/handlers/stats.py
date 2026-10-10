from datetime import timedelta

from aiogram import F, Router
from aiogram.types import Message
from sqlalchemy import func, select

from app.bot.common import esc
from app.bot.handlers.leads import category_name
from app.db import Session
from app.models import STAGE_NAMES, Client, Group, Lead, now

router = Router()


async def build_report() -> str:
    async with Session() as s:
        day, week = now() - timedelta(days=1), now() - timedelta(days=7)
        count = lambda *w: s.scalar(select(func.count()).select_from(Lead).where(*w))  # noqa: E731
        today, wk, total = await count(Lead.created_at > day), await count(Lead.created_at > week), await count()
        taken = await count(Lead.created_at > day, Lead.status == "taken")
        stages = dict((await s.execute(select(Client.stage, func.count()).group_by(Client.stage))).all())
        done_sum = await s.scalar(select(func.coalesce(func.sum(Client.budget), 0)).where(Client.stage == "done"))
        cats = (await s.execute(
            select(Lead.category_id, func.count()).where(Lead.created_at > week)
            .group_by(Lead.category_id).order_by(func.count().desc()).limit(5)
        )).all()
        groups = (await s.scalars(select(Group).where(Group.leads_count > 0).order_by(Group.leads_count.desc()).limit(5))).all()
        lines = [
            f"📊 Заявок: за сутки {today}, за неделю {wk}, всего {total}; в работу взято за сутки: {taken}",
            "Клиенты: " + (", ".join(f"{STAGE_NAMES[k]} {v}" for k, v in stages.items()) or "нет"),
            f"💰 Сумма выполненных: {done_sum}",
        ]
        if cats:
            lines.append("\nПо категориям (неделя):")
            lines += [f"• {esc(await category_name(s, cid))}: {n}" for cid, n in cats]
        if groups:
            lines.append("\n🏆 Рейтинг групп:")
            lines += [f"• {esc(g.title)}: {g.leads_count}" for g in groups]
    return "\n".join(lines)


@router.message(F.text == "📊 Статистика")
async def stats(m: Message) -> None:
    await m.answer(await build_report())
