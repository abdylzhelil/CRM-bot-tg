"""Действия над заявкой пользователя — общая логика для бота (кнопки под карточкой) и мини-приложения."""
from sqlalchemy import func, select

from app.models import BlacklistEntry, Category, Client, ClientEvent, Delivery, Group, Lead, SpamVote, User
from app.services import draft, settings, spamtexts, templates
from app.services.distribute import root_category

STATUS_NOTES = {"later": ("postponed", "Отложено"), "reject": ("rejected", "Отказ")}
ACTIONS = ("take", "later", "reject", "spam", "client", "restore")
RESTORABLE = ("postponed", "rejected", "spam")


class ActionError(Exception):
    pass


def contact_url(lead: Lead) -> str:
    return f"https://t.me/{lead.username}" if lead.username else f"tg://user?id={lead.author_id}"


async def blacklist(s, lead: Lead, status: str, reason: str) -> None:
    entry = await s.get(BlacklistEntry, lead.author_id)
    if entry:
        entry.status, entry.reason = status, reason
    else:
        s.add(BlacklistEntry(user_id=lead.author_id, username=lead.username, status=status, reason=reason))


async def vote_spam(s, lead: Lead, user: User) -> bool:
    """Голос «спам»: пользователю автор больше не приходит; админ или N голосов — общий чёрный список,
    плюс текст запоминается как образец (похожие с других аккаунтов тоже отсеются). True — заблокирован для всех.

    Чтобы не нажимать «спам» дважды: другие заявки этого автора, которые ещё лежат у человека новыми или
    ждут отправки, сразу уходят в «спам» (их можно вернуть из списка 🚫 Спам)."""
    if await s.get(SpamVote, (lead.author_id, user.tg_id)) is None:
        s.add(SpamVote(author_id=lead.author_id, user_id=user.tg_id))
        await s.flush()
    others = (await s.scalars(select(Delivery).join(Lead, Lead.id == Delivery.lead_id).where(
        Lead.author_id == lead.author_id, Delivery.user_id == user.tg_id, Lead.id != lead.id,
        Delivery.status.in_(("new", "postponed"))))).all()
    for other in others:
        other.status, other.sent = "spam", True
    votes = await s.scalar(select(func.count()).select_from(SpamVote).where(SpamVote.author_id == lead.author_id))
    if not (user.is_admin or votes >= await settings.get_int(s, "spam_votes_to_ban")):
        return False
    await blacklist(s, lead, "spam", "отмечено администратором" if user.is_admin else f"голосов «спам»: {votes}")
    await spamtexts.remember(s, lead.text, lead.id, user.tg_id)
    # у остальных ещё не отправленные заявки этого автора не придут
    pending = (await s.scalars(select(Delivery).join(Lead, Lead.id == Delivery.lead_id).where(
        Lead.author_id == lead.author_id, ~Delivery.sent))).all()
    for d in pending:
        d.status, d.sent = "spam", True
    return True


async def take(s, user: User, d: Delivery, lead: Lead) -> Client:
    """«В работу»: клиент попадает в CRM пользователя."""
    client = await s.scalar(select(Client).where(Client.user_id == user.tg_id, Client.tg_id == lead.author_id))
    if not client:
        group = await s.get(Group, lead.group_id) if lead.group_id else None
        client = Client(
            user_id=user.tg_id, tg_id=lead.author_id, name=lead.name, username=lead.username,
            source=group.title if group else lead.group_title, category_id=lead.category_id,
        )
        s.add(client)
        await s.flush()
    s.add(ClientEvent(client_id=client.id, text=f"Заявка: {lead.text[:300]}"))
    d.status = "taken"
    return client


async def apply_status(s, user: User, d: Delivery, lead: Lead, action: str) -> str:
    """later / reject / spam / client. Возвращает подпись результата. Коммит — на вызывающем."""
    if action in STATUS_NOTES:
        d.status, note = STATUS_NOTES[action]
        return note
    if action == "spam":
        d.status = "spam"
        if await vote_spam(s, lead, user):
            return "Спам: автор и похожие тексты больше не придут"
        return "Это спам — автор больше не придёт"
    if action == "restore":
        return await restore(s, user, d, lead)
    if action == "client" and user.is_admin:
        await blacklist(s, lead, "client", "отмечено администратором")
        lead.suspicious = False
        return "В белом списке"
    raise ActionError("Действие недоступно")


async def restore(s, user: User, d: Delivery, lead: Lead) -> str:
    """↩️ Вернуть в новые (после «Отложить», «Отказ» или «Спам» по ошибке).

    Для «спама» снимаем голос этого пользователя; если автор попал в общий чёрный список из-за голосов
    и голосов стало меньше порога (или вернул администратор) — убираем его оттуда."""
    if d.status not in RESTORABLE:
        raise ActionError("Эту заявку вернуть нельзя")
    if d.status == "spam":
        vote = await s.get(SpamVote, (lead.author_id, user.tg_id))
        if vote is not None:
            await s.delete(vote)
            await s.flush()
        entry = await s.get(BlacklistEntry, lead.author_id)
        if entry is not None and entry.status == "spam":
            votes = await s.scalar(select(func.count()).select_from(SpamVote).where(SpamVote.author_id == lead.author_id))
            if user.is_admin or votes < await settings.get_int(s, "spam_votes_to_ban"):
                await s.delete(entry)
                await spamtexts.forget_lead(s, lead.id)
    d.status = "new"
    return "Вернул в новые"


async def own_delivery(s, user: User, delivery_id: int):
    d = await s.get(Delivery, delivery_id)
    lead = await s.get(Lead, d.lead_id) if d else None
    if not d or not lead or d.user_id != user.tg_id:
        return None, None
    return d, lead


async def template_text(s, user: User, lead: Lead) -> str:
    """Ответ клиенту: шаблон и цены пользователя; у администратора — ещё и из категории."""
    cat = await s.get(Category, lead.category_id) if lead.category_id else None
    parent = await s.get(Category, cat.parent_id) if cat and cat.parent_id else None
    tpl, prices = user.reply_template, user.prices
    if user.is_admin:
        tpl = tpl or (cat.reply_template if cat else "") or (parent.reply_template if parent else "")
        prices = prices or (cat.prices if cat else "") or (parent.prices if parent else "")
    return templates.render(
        tpl, name=lead.name, group=lead.group_title,
        category=(cat.name.lower() if cat else "эта услуга"), prices=prices,
    )


async def rule_draft(s, user: User, lead: Lead) -> str:
    """Бесплатный черновик по правилам (без внешних сервисов)."""
    root_id = await root_category(s, lead.category_id)
    root = (await s.get(Category, root_id)).name if root_id else ""
    return draft.build(lead.text, lead.name, root, user.reply_template, user.prices)
