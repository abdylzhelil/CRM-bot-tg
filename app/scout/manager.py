"""Разведчики: читают сообщения групп (только чтение!), следят за здоровьем сессий."""
import asyncio
import logging

from sqlalchemy import select
from telethon import TelegramClient, events
from telethon.errors import (
    AuthKeyUnregisteredError,
    SessionRevokedError,
    UserDeactivatedBanError,
    UserDeactivatedError,
)
from telethon.tl.types import User

from app.bot import notify
from app.db import Session
from app.models import ScoutAccount
from app.scout.client import make_client
from app.services.antispam import AuthorInfo
from app.services import intake
from app.services.pipeline import Incoming

log = logging.getLogger(__name__)
DEAD = (AuthKeyUnregisteredError, SessionRevokedError, UserDeactivatedBanError, UserDeactivatedError)


class ScoutManager:
    def __init__(self) -> None:
        self.clients: dict[int, TelegramClient] = {}

    async def start(self) -> None:
        async with Session() as s:
            accounts = (await s.scalars(select(ScoutAccount).where(ScoutAccount.enabled))).all()
        for acc in accounts:
            if acc.session:
                await self.attach(acc)

    async def attach(self, acc: ScoutAccount) -> None:
        """Запустить (или перезапустить) аккаунт: можно вызывать из бота без правки кода."""
        old = self.clients.pop(acc.id, None)
        if old:
            await old.disconnect()
        client = make_client(acc)
        try:
            await client.connect()
            if not await client.is_user_authorized():
                raise AuthKeyUnregisteredError(request=None)
        except DEAD as e:
            await self._mark(acc.id, "dead", f"Сессия разведчика «{acc.name}» не авторизована: {type(e).__name__}")
            return
        except Exception:
            log.exception("не удалось подключить %s", acc.name)
            await notify.alert(f"Не удалось подключить разведчика «{acc.name}» (см. логи). Повторю при следующей проверке.")
            return
        try:  # запоминаем Telegram ID аккаунта: его рекламные посты в группах не должны стать «заявками»
            me = await client.get_me()
            async with Session() as s:
                row = await s.get(ScoutAccount, acc.id)
                if row is not None and me is not None and row.tg_user_id != me.id:
                    row.tg_user_id = me.id
                    await s.commit()
        except Exception:
            log.warning("не удалось узнать Telegram ID разведчика %s", acc.name, exc_info=True)
        client.add_event_handler(self._on_message, events.NewMessage(incoming=True))
        self.clients[acc.id] = client
        await self._mark(acc.id, "ok")
        asyncio.create_task(self._tidy(client, acc.id))
        log.info("разведчик %s запущен", acc.name)

    async def _tidy(self, client: TelegramClient, acc_id: int) -> None:
        """При запуске разложить уже вступленные группы по папкам."""
        from app.scout.folders import sync_folders

        try:
            await sync_folders(client, acc_id)
        except Exception:
            log.warning("папки разведчика #%s не обновлены", acc_id, exc_info=True)

    async def detach(self, acc_id: int) -> None:
        client = self.clients.pop(acc_id, None)
        if client:
            await client.disconnect()

    async def _mark(self, acc_id: int, status: str, alert: str = "") -> None:
        async with Session() as s:
            acc = await s.get(ScoutAccount, acc_id)
            if acc and acc.status != status:
                acc.status = status
                await s.commit()
            elif not acc:
                return
        if alert:
            await notify.alert(alert)

    async def _on_message(self, event: events.NewMessage.Event) -> None:
        try:
            if not event.is_group or not event.raw_text or len(event.raw_text) < 8:
                return
            sender = await event.get_sender()
            if not isinstance(sender, User):
                return
            chat = await event.get_chat()
            title = getattr(chat, "title", "") or ""
            username = getattr(chat, "username", None)
            link = (
                f"https://t.me/{username}/{event.id}" if username
                else f"https://t.me/c/{getattr(chat, 'id', 0)}/{event.id}"
            )
            msg = Incoming(
                text=event.raw_text,
                author=AuthorInfo(
                    user_id=sender.id,
                    username=sender.username or "",
                    first_name=sender.first_name or "",
                    last_name=sender.last_name or "",
                    bot=bool(sender.bot),
                    scam=bool(sender.scam),
                    fake=bool(sender.fake),
                    deleted=bool(sender.deleted),
                    has_photo=sender.photo is not None,
                ),
                group_tg_id=event.chat_id,
                group_title=title,
                message_id=event.id,
                link=link,
            )
            await intake.handle(msg)
        except Exception:
            log.exception("ошибка обработки сообщения")

    async def watchdog(self) -> None:
        """Каждые 5 минут проверяет сессии; при обрыве — переподключает, при бане — шлёт алерт."""
        while True:
            await asyncio.sleep(300)
            for acc_id, client in list(self.clients.items()):
                try:
                    try:
                        if not client.is_connected():
                            await client.connect()
                        await client.get_me()
                    except DEAD as e:
                        await self.detach(acc_id)
                        await self._mark(acc_id, "dead", f"Аккаунт-разведчик #{acc_id} вылетел: {type(e).__name__}. Нужен повторный вход.")
                except Exception:  # в том числе ошибка базы в _mark — сторож не должен остановиться
                    log.exception("проверка разведчика #%s", acc_id)
