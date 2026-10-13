"""Чтение и сохранение личных настроек пользователя — общая логика для мини-приложения (бот правит те же поля по одному)."""
from sqlalchemy import delete, select

from app.models import Category, User, UserCategory, UserNegative
from app.services import quiet

MAX_TEXT = 2000
MAX_WORDS = 50


class ValidationError(Exception):
    pass


async def get_profile(s, user: User) -> dict:
    user = await s.get(User, user.tg_id)  # свежие данные из базы, а не копия из начала запроса
    roots = (await s.scalars(
        select(Category).where(Category.parent_id.is_(None), Category.enabled, ~Category.pending).order_by(Category.id)
    )).all()
    mine = set((await s.scalars(select(UserCategory.category_id).where(UserCategory.user_id == user.tg_id))).all())
    negatives = (await s.scalars(select(UserNegative.word).where(UserNegative.user_id == user.tg_id).order_by(UserNegative.id))).all()
    return {
        "niches": [{"id": c.id, "name": c.name, "emoji": c.emoji, "on": c.id in mine} for c in roots],
        "city": user.city,
        "only_city": user.only_city,
        "quiet_hours": user.quiet_hours,
        "negatives": list(negatives),
        "reply_template": user.reply_template,
        "prices": user.prices,
        "paused": user.paused,
    }


async def update_profile(s, user: User, data: dict) -> list[str]:
    """Применяет только присланные поля. Возвращает список изменённых. Бросает ValidationError с понятным текстом."""
    row = await s.get(User, user.tg_id)
    changed: list[str] = []
    if "niches" in data:
        ids = data["niches"]
        if not isinstance(ids, list) or not all(isinstance(i, int) for i in ids):
            raise ValidationError("Ниши: нужен список номеров")
        valid = set((await s.scalars(select(Category.id).where(Category.id.in_(ids), Category.parent_id.is_(None),
                                                               Category.enabled, ~Category.pending))).all())
        if not valid:
            raise ValidationError("Выберите хотя бы одну нишу")
        await s.execute(delete(UserCategory).where(UserCategory.user_id == row.tg_id))
        for cid in valid:
            s.add(UserCategory(user_id=row.tg_id, category_id=cid))
        changed.append("niches")
    if "city" in data:
        city = str(data["city"]).strip()[:64]
        row.city = city
        if not city:
            row.only_city = False
        changed.append("city")
    if "only_city" in data:
        row.only_city = bool(data["only_city"]) and bool(row.city)
        changed.append("only_city")
    if "quiet_hours" in data:
        text = str(data["quiet_hours"]).strip()
        if text in ("-", "0", "выкл"):
            text = ""
        elif not quiet.parse_hours(text):
            raise ValidationError("Тишина: формат 23-8 (с 23:00 до 08:00) или пусто, чтобы выключить")
        row.quiet_hours = text
        changed.append("quiet_hours")
    for field in ("reply_template", "prices"):
        if field in data:
            text = str(data[field]).strip()
            if len(text) > MAX_TEXT:
                raise ValidationError(f"Слишком длинный текст (до {MAX_TEXT} символов)")
            setattr(row, field, text)
            changed.append(field)
    if "paused" in data:
        row.paused = bool(data["paused"])
        changed.append("paused")
    if "negatives" in data:
        words = data["negatives"]
        if not isinstance(words, list) or len(words) > MAX_WORDS:
            raise ValidationError(f"Минус-слова: список до {MAX_WORDS} слов")
        clean, seen = [], set()
        for w in words:
            w = str(w).strip()[:64]
            if w and w.lower() not in seen:
                seen.add(w.lower())
                clean.append(w)
        await s.execute(delete(UserNegative).where(UserNegative.user_id == row.tg_id))
        for w in clean:
            s.add(UserNegative(user_id=row.tg_id, word=w))
        changed.append("negatives")
    await s.commit()
    return changed
