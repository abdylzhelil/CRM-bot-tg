"""Порядок в Telegram разведчика: все его группы — в отдельных папках «🔍 Разведка N», звук выключен.

В папку Telegram помещается до 100 чатов (без Premium), поэтому групп больше 100 — несколько папок.
"""
import logging
from datetime import datetime

from sqlalchemy import select
from telethon import TelegramClient, functions, types, utils

from app.db import Session
from app.models import Group

log = logging.getLogger(__name__)

FIRST_FOLDER_ID = 10  # id папок 10…14, чтобы не задеть папки, созданные вручную
MAX_FOLDERS = 5
PER_FOLDER = 100
MUTE_FOREVER = datetime(2038, 1, 1)


def folder_title(n: int) -> str:
    return f"🔍 Разведка {n}" if n > 1 else "🔍 Разведка"


async def mute(client: TelegramClient, entity) -> None:
    """Выключить уведомления группы — телефон разведчика не будет звенеть."""
    try:
        await client(functions.account.UpdateNotifySettingsRequest(
            peer=types.InputNotifyPeer(peer=await client.get_input_entity(entity)),
            settings=types.InputPeerNotifySettings(mute_until=MUTE_FOREVER),
        ))
    except Exception:
        log.warning("не удалось выключить звук группы", exc_info=True)


async def sync_folders(client: TelegramClient, acc_id: int) -> int:
    """Разложить группы аккаунта по папкам. Возвращает, сколько групп в папках."""
    async with Session() as s:
        ids = (await s.scalars(
            select(Group.tg_id).where(Group.account_id == acc_id, Group.status == "joined").order_by(Group.joined_at)
        )).all()
    peers_by_id = {}
    async for dialog in client.iter_dialogs():
        peers_by_id[utils.get_peer_id(dialog.entity)] = dialog.input_entity
    peers = [peers_by_id[i] for i in ids if i in peers_by_id][: PER_FOLDER * MAX_FOLDERS]
    chunks = [peers[i : i + PER_FOLDER] for i in range(0, len(peers), PER_FOLDER)]
    for n in range(MAX_FOLDERS):
        folder_id = FIRST_FOLDER_ID + n
        try:
            if n < len(chunks):
                await client(functions.messages.UpdateDialogFilterRequest(
                    id=folder_id,
                    filter=types.DialogFilter(
                        id=folder_id,
                        title=types.TextWithEntities(text=folder_title(n + 1), entities=[]),
                        pinned_peers=[], include_peers=chunks[n], exclude_peers=[], emoticon="🔍",
                    ),
                ))
            else:
                await client(functions.messages.UpdateDialogFilterRequest(id=folder_id))  # удалить лишнюю
        except Exception:
            log.warning("папка %s: не удалось обновить", folder_id, exc_info=True)
    return len(peers)
