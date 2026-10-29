"""Обходчик: нажимает все меню, команды и кнопки бота от лица новичка, пользователя с 💎 и админа.

Ловит то, что видит человек как «бот сломался»: ошибку в обработчике, сообщение, которое Telegram не примет
(длина, неверная HTML-разметка, данные кнопки длиннее 64 байт, пустой ряд кнопок, всплывашка > 200 знаков),
и кнопку, нажатие на которую ничего не отвечает (у человека бесконечно крутятся часики).
"""
import asyncio
import re
from datetime import timedelta
from html.parser import HTMLParser

import pytest
from aiogram.methods import AnswerCallbackQuery, EditMessageReplyMarkup, EditMessageText, SendMessage, SendPhoto

from tests.conftest import ADMIN_ID
from tests.test_flow import FakeSession, app_bot, cb_update, text_update  # noqa: F401  (фикстура)


class CrawlSession(FakeSession):
    """Отвечает на запросы о группе как настоящий Telegram (бот — обычный участник)."""

    async def make_request(self, bot, method, timeout=None):
        from aiogram.methods import GetChat, GetChatMember, GetChatMemberCount
        from aiogram.types import ChatFullInfo, ChatMemberMember, User

        if isinstance(method, GetChatMember):
            self.requests.append(method)
            return ChatMemberMember(user=User(id=method.user_id, is_bot=False, first_name="x"))
        if isinstance(method, GetChatMemberCount):
            self.requests.append(method)
            return 1500
        if isinstance(method, GetChat):
            self.requests.append(method)
            return ChatFullInfo(id=method.chat_id if isinstance(method.chat_id, int) else -1, type="supergroup",
                                title="Группа", accent_color_id=0, max_reaction_count=0, accepted_gift_types={
                                    "unlimited_gifts": False, "limited_gifts": False, "unique_gifts": False,
                                    "premium_subscription": False})
        return await super().make_request(bot, method, timeout)

ALLOWED = {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "a", "code", "pre", "tg-spoiler", "span",
           "blockquote", "tg-emoji"}
NEW, FREE, PAID = 9001, 9002, 9003


class _Html(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.stack, self.problems = [], []

    def handle_starttag(self, tag, attrs):
        if tag not in ALLOWED:
            self.problems.append(f"тег <{tag}> Telegram не понимает")
        self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack[-1] != tag:
            self.problems.append(f"лишний/непарный </{tag}>")
        else:
            self.stack.pop()


def html_problems(text: str) -> list[str]:
    p = _Html()
    p.feed(text or "")
    p.close()
    out = p.problems + ([f"не закрыт <{p.stack[-1]}>"] if p.stack else [])
    if re.search(r"<(?![/a-zA-Z])", text or ""):
        out.append("голый «<» — Telegram не примет")
    return out


def check_request(r) -> list[str]:
    bad = []
    text = getattr(r, "text", None) if isinstance(r, SendMessage | EditMessageText) else getattr(r, "caption", None)
    mode = getattr(r, "parse_mode", "HTML")
    if text is not None:
        if len(text) > (4096 if not isinstance(r, SendPhoto) else 1024):
            bad.append(f"текст {len(text)} знаков — больше лимита Telegram")
        if mode is not None:  # без разметки (parse_mode=None) «<» допустим
            bad += html_problems(text)
        if not text.strip():
            bad.append("пустой текст")
    if isinstance(r, AnswerCallbackQuery) and r.text and len(r.text) > 200:
        bad.append(f"всплывашка {len(r.text)} знаков (>200)")
    markup = getattr(r, "reply_markup", None)
    for row in getattr(markup, "inline_keyboard", None) or []:
        if not row:
            bad.append("пустой ряд кнопок")
        for b in row:
            if not (b.text or "").strip():
                bad.append("кнопка без текста")
            if b.callback_data and len(b.callback_data.encode()) > 64:
                bad.append(f"данные кнопки длиннее 64 байт: {b.callback_data}")
    return bad


def callbacks_of(r) -> list[str]:
    markup = getattr(r, "reply_markup", None)
    return [b.callback_data for row in getattr(markup, "inline_keyboard", None) or [] for b in row if b.callback_data]


async def seed_world(maker):
    """Немного жизни в базе: группы, заявки, клиенты, оплата, заказ рекламы, сообщения, кредиты."""
    from sqlalchemy import select

    from app.models import (
        AdOrder, Category, Client, Delivery, Feedback, Group, Lead, Payment, ScoutAccount, User, UserCategory, now,
    )
    from app.services import credits

    async with maker() as s:
        cat = await s.scalar(select(Category).where(Category.parent_id.is_(None)).limit(1))
        s.add_all([
            User(tg_id=FREE, name="Мастер <Иван> & Ко", username="ivan", onboarded=True, city="Махачкала", credits=600),
            User(tg_id=PAID, name="Платный", onboarded=True, plan="full", plan_until=now() + timedelta(days=20),
                 renew_method="pm", renew_card="Mir •••• 4242", renew_days=30),
        ])
        await s.flush()
        for uid in (FREE, PAID, ADMIN_ID):
            if await s.get(UserCategory, (uid, cat.id)) is None:
                s.add(UserCategory(user_id=uid, category_id=cat.id))
        acc = ScoutAccount(name="Разведчик", api_id=1, api_hash="x", phone="+79000000000")
        s.add(acc)
        await s.flush()
        groups = [
            Group(tg_id=-100 - i, title=f"Барахолка <{i}> Махачкала", username=f"bar{i}", status=st, city="Махачкала",
                  region="Дагестан", members=1000 + i, catalog=i == 0, submitted_by=FREE if i == 0 else 0,
                  category_id=cat.id, ads_allowed=i == 1, ad_account_id=acc.id if i == 1 else 0, bot_member=i == 2,
                  board_admin=FREE if i == 2 else 0, account_id=acc.id, admins="adm" if i == 3 else "",
                  captcha_since=now() if i == 4 else None, offer_status="sent" if i == 3 else "")
            for i, st in enumerate(["joined", "joined", "joined", "joined", "joined", "manual", "skipped", "pending",
                                    "requested", "left", "approved", "candidate"])
        ]
        s.add_all(groups)
        await s.flush()
        for i in range(6):
            lead = Lead(author_id=500 + i, username="client" if i % 2 else "", name="Клиент <А>", group_title="Барахолка", text=f"Ищу мастера <срочно> & недорого {i}",
                        text_hash=f"crawl{i}", category_id=cat.id, group_id=groups[0].id, city="Махачкала",
                        region="Дагестан", link=f"https://t.me/bar0/{i}", matched="мастер")
            s.add(lead)
            await s.flush()
            for uid, st in ((FREE, "new"), (PAID, ["new", "taken", "later", "rejected", "spam", "new"][i]),
                            (ADMIN_ID, "new")):
                s.add(Delivery(lead_id=lead.id, user_id=uid, status=st, sent=True))
        s.add(Client(user_id=PAID, name="Ирина", phone="+79001112233"))
        s.add(Payment(user_id=PAID, days=30, amount=49000, charge_id="crawl-1", provider_id="yk-crawl"))
        s.add(AdOrder(user_id=FREE, text="Ремонт квартир <под ключ>", status="pending", price=300, group_ids=str(groups[1].id)))
        s.add(Feedback(user_id=FREE, text="Привет <b>владелец</b>"))
        await credits.add_codes(s, "monthly", "AAAA-BBBB-CCCC")
        from app.services import settings

        for key, val in (("price_30", "490"), ("price_90", "1290"), ("price_365", "3990"), ("adpost_enabled", "1"),
                         ("adpost_price", "300")):
            await settings.put(s, key, val)  # как на сервере: тариф продаётся, реклама включена
        await s.commit()


async def crawl(bot, dp, session, uid: int, start: list[str], limit: int, errors: list, monkeypatch) -> dict:
    """Отправить стартовые сообщения и нажимать все новые кнопки (в ширину). → {что: [проблемы]}."""
    problems: dict[str, list[str]] = {}
    seen: set[str] = set()
    shapes: dict[str, int] = {}  # «вид» кнопки (цифры → N): по 3 каждого вида, чтобы пройти все разделы
    queue: list[str] = []
    clicks = 0

    async def feed(label: str, update) -> None:
        before = len(session.requests)
        errors.clear()
        try:
            await asyncio.wait_for(dp.feed_update(bot, update), 15)
        except asyncio.TimeoutError:
            problems.setdefault(label, []).append("думает дольше 15 с")
        new = session.requests[before:]
        for e in errors:
            problems.setdefault(label, []).append(f"ОШИБКА: {e}")
        for r in new:
            for p in check_request(r):
                problems.setdefault(label, []).append(p)
            for d in callbacks_of(r):
                shape = re.sub(r"\d+", "N", d)
                if d not in seen and (shapes.get(shape, 0) < 3 or d.startswith("cap:")):  # капчу — все значки
                    seen.add(d)
                    shapes[shape] = shapes.get(shape, 0) + 1
                    queue.append(d)
        if label.startswith(("cb ", "msg ")) and not new:
            problems.setdefault(label, []).append("кнопка ничего не отвечает")

    for text in start:
        await feed(f"msg {text}", text_update(uid, text))
        await feed("msg /cancel", text_update(uid, "/cancel"))
    inputs: set[str] = set()
    while queue and clicks < limit:
        data = queue.pop(0)
        clicks += 1
        await feed(f"cb {data}", cb_update(uid, data))
        ctx = dp.fsm.get_context(bot=bot, chat_id=uid, user_id=uid)
        st = await ctx.get_state()
        action = (await ctx.get_data()).get("action") if st else None
        if action:  # бот ждёт ввода — пишем «трудный» текст, потом цифры; ошибок быть не должно
            if action not in inputs:
                inputs.add(action)
                for sample in ("Тест <b>не тег</b> & 5 < 7 @someone https://t.me/x", "12345"):
                    await feed(f"ввод «{action}» после {data}", text_update(uid, sample))
                    if not await ctx.get_state():
                        break
            await ctx.clear()
    print(f"  вводов проверено: {len(inputs)}: {', '.join(sorted(inputs))}")
    return {"problems": problems, "clicks": clicks, "left": len(queue)}


@pytest.mark.parametrize("who", ["new", "free", "paid", "admin"])
async def test_crawl_everything(app_bot, maker, tgbot, monkeypatch, who):  # noqa: F811
    from app.bot import notify, setup
    from app.bot.common import ADMIN_MENU, ADMIN_SECTIONS, USER_MENU

    bot, dp, _ = app_bot
    session = CrawlSession()
    bot.session = session
    await seed_world(maker)
    errors: list[str] = []

    class Log:
        def error(self, msg, *a, exc_info=None, **k):
            errors.append(f"{type(exc_info).__name__}: {str(exc_info)[:400]}" if exc_info else msg)

        def __getattr__(self, name):
            return lambda *a, **k: None

    monkeypatch.setattr(setup, "log", Log())

    async def no_alert(*a, **k):
        return None

    monkeypatch.setattr(notify, "alert_once", no_alert)
    commands = [f"/{c.command}" for c in setup.USER_COMMANDS] + ["/start", "/help", "/credits", "/about", "/rating"]
    uid = {"new": NEW, "free": FREE, "paid": PAID, "admin": ADMIN_ID}[who]
    start = ["/start"]
    if who == "new":
        start = ["/start", "/start src_g3_c1", "привет"]
    else:
        start += commands + USER_MENU
        if who == "admin":
            start += ["🛠 Управление"] + ADMIN_SECTIONS + [f"/{c.command}" for c in setup.ADMIN_COMMANDS
                                                           if c.command not in ("aitest", "forget")]
    res = await crawl(bot, dp, session, uid, list(dict.fromkeys(start)), 700, errors, monkeypatch)
    report = "\n".join(f"{k}: {'; '.join(dict.fromkeys(v))}" for k, v in res["problems"].items())
    import os

    if os.getenv("CRAWL_DUMP"):  # выгрузить все экраны для чтения глазами
        with open(os.path.join(os.getenv("CRAWL_DUMP"), f"{who}.txt"), "w") as f:
            seen_t = set()
            for r in session.requests:
                txt = getattr(r, "text", None) or getattr(r, "caption", None)
                if txt and txt not in seen_t and getattr(r, "chat_id", uid) == uid:
                    seen_t.add(txt)
                    btns = [b.text for row in getattr(getattr(r, "reply_markup", None), "inline_keyboard", None) or [] for b in row]
                    f.write(f"===== {type(r).__name__}\n{txt}\n" + (f"[кнопки] {' | '.join(btns)}\n" if btns else ""))
    print(f"\n[{who}] нажатий {res['clicks']}, не дошли {res['left']}\n{report}")
    assert not res["problems"], report
