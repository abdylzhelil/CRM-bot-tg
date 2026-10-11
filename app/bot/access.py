"""Пользователи бота: регистрация при первом сообщении, права администратора."""
from aiogram import BaseMiddleware
from aiogram.filters import BaseFilter

from app import config
from app.db import Session
from app.models import User, now


class UserMiddleware(BaseMiddleware):
    """В личке: находит или создаёт пользователя и кладёт его в data["user"].

    Сообщения групп проходят без пользователя — их обрабатывает только ingroup.router.
    """

    async def __call__(self, handler, event, data):
        chat = data.get("event_chat")
        tg_user = data.get("event_from_user")
        if chat and chat.type != "private":
            return await handler(event, data)
        if tg_user is None or tg_user.is_bot:
            return None
        async with Session() as s:
            user = await s.get(User, tg_user.id)
            if user is None:
                user = User(tg_id=tg_user.id, is_admin=tg_user.id in config.OWNER_IDS)
                if user.is_admin:
                    user.plan = "full"
                s.add(user)
            user.name = tg_user.full_name[:255]
            user.username = tg_user.username or ""
            user.last_seen = now()
            user.blocked = False  # написал боту — значит, снова доступен
            if tg_user.id in config.OWNER_IDS:
                user.is_admin = True
            await s.commit()
        data["user"] = user
        return await handler(event, data)


class IsAdmin(BaseFilter):
    async def __call__(self, event, user: User | None = None) -> bool:
        return user is not None and user.is_admin
