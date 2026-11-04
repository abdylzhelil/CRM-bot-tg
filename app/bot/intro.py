"""Радар-анимация для новых пользователей на /start. Файл лежит в образе; после первой отправки Telegram отдаёт file_id."""
import logging
from pathlib import Path

from aiogram.types import FSInputFile, Message

log = logging.getLogger(__name__)
GIF = Path(__file__).resolve().parent.parent / "assets" / "radar.gif"
_file_id: str | None = None


async def send_radar(m: Message) -> None:
    """Показать анимацию один раз за заход; любая ошибка (нет файла, Telegram отказал) не мешает регистрации."""
    global _file_id
    try:
        sent = await m.answer_animation(_file_id or FSInputFile(GIF), caption="📡 КлиентРадар ищет клиентов для вас")
        if sent.animation and not _file_id:
            _file_id = sent.animation.file_id
    except Exception:
        log.warning("не удалось отправить радар-анимацию", exc_info=True)
