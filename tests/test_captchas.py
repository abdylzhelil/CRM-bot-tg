"""🧩 Капча: найти группы, где разведчику нельзя писать, дать ссылку на сообщение бота, выйти, если не прошли."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy import select

from app.models import Group, ScoutAccount, now
from app.scout import captchas


class Client:
    def __init__(self, restricted=True, left=False):
        self.restricted, self.left, self.calls = restricted, left, []

    async def get_input_entity(self, _):
        return object()

    async def get_entity(self, _):
        rights = SimpleNamespace(send_messages=self.restricted, send_plain=False)
        return SimpleNamespace(banned_rights=rights, left=self.left)

    async def get_me(self):
        return SimpleNamespace(id=555)

    async def iter_messages(self, entity, limit=60):
        base = datetime.now(timezone.utc)
        yield SimpleNamespace(id=10, date=base, sender=SimpleNamespace(bot=False), message="привет всем", mentioned=False)
        yield SimpleNamespace(id=9, date=base, sender=SimpleNamespace(bot=True), mentioned=True,
                              message="Нажмите кнопку, чтобы подтвердить, что вы не бот")

    async def __call__(self, req):
        self.calls.append(type(req).__name__)
        return SimpleNamespace(link="https://t.me/c/1/9")


async def _group(maker, **kw):
    async with maker() as s:
        s.add(ScoutAccount(id=1, name="sc", api_id=1, api_hash="x"))
        await s.flush()
        g = Group(tg_id=-901, title="Барахолка", status="joined", account_id=1, joined_at=now(), **kw)
        s.add(g)
        await s.commit()
        return g.id


def test_restricted_is_own_rights_only():
    assert captchas.restricted(SimpleNamespace(banned_rights=SimpleNamespace(send_messages=True)))
    # группа закрыта для всех (default_banned_rights) — это не капча
    assert not captchas.restricted(SimpleNamespace(banned_rights=None,
                                                   default_banned_rights=SimpleNamespace(send_messages=True)))


async def test_captcha_found_with_link_and_owner_told(maker, tgbot, monkeypatch):
    monkeypatch.setattr(captchas.asyncio, "sleep", AsyncMock())
    gid = await _group(maker)
    manager = SimpleNamespace(clients={1: Client()})
    assert await captchas.captcha_round(manager) == {"captcha": 1}
    async with maker() as s:
        g = await s.get(Group, gid)
        assert g.captcha_since and g.captcha_link == "https://t.me/c/1/9"
        assert await captchas.count(s) == 1
    assert any("Нужно пройти капчу" in t for t in tgbot.texts(1))
    # прошли капчу — отметка снимается
    async with maker() as s:
        g = await s.get(Group, gid)
        g.captcha_checked_at = None
        await s.commit()
    manager.clients[1] = Client(restricted=False)
    assert await captchas.check_one(manager, gid) == "passed"
    async with maker() as s:
        assert (await s.get(Group, gid)).captcha_since is None


async def test_leaves_after_deadline_but_not_catalog(maker):
    from app.services import settings

    async with maker() as s:  # по умолчанию 0 — не выходить; владелец включил 3 дня
        assert await settings.get(s, "captcha_leave_days") == "0"
        await settings.put(s, "captcha_leave_days", "3")
    gid = await _group(maker, captcha_since=now() - timedelta(days=4))
    client = Client()
    assert await captchas.check_one(SimpleNamespace(clients={1: client}), gid) == "left"
    async with maker() as s:
        g = await s.get(Group, gid)
    assert g.status == "left" and "капчу не прошли" in g.skip_reason and "LeaveChannelRequest" in client.calls
    async with maker() as s:  # группа каталога — не выходим
        g = await s.get(Group, gid)
        g.status, g.catalog, g.captcha_checked_at = "joined", True, None
        g.captcha_since = now() - timedelta(days=4)
        await s.commit()
    assert await captchas.check_one(SimpleNamespace(clients={1: Client()}), gid) == "still"


async def test_kicked_goes_to_left(maker, tgbot):
    gid = await _group(maker)
    assert await captchas.check_one(SimpleNamespace(clients={1: Client(left=True)}), gid) == "kicked"
    async with maker() as s:
        assert (await s.get(Group, gid)).status == "left"
        g = await s.get(Group, gid)  # выкинули из-за капчи — не теряем, «вступить вручную»
        g.status, g.captcha_since, g.captcha_checked_at = "joined", now(), None
        await s.commit()
    assert await captchas.check_one(SimpleNamespace(clients={1: Client(left=True)}), gid) == "kicked"
    async with maker() as s:
        assert (await s.get(Group, gid)).status == "manual"


async def test_captcha_closes_ads_until_passed(maker, tgbot):
    from app.services import adposts

    gid = await _group(maker, ads_allowed=True, ad_account_id=1)
    async with maker() as s:
        acc = await s.get(ScoutAccount, 1)
        acc.enabled, acc.can_post = True, True
        await s.commit()
        assert [g.id for g in await adposts.eligible_groups(s)] == [gid]
    assert await captchas.check_one(SimpleNamespace(clients={1: Client()}), gid) == "captcha"
    async with maker() as s:
        assert await adposts.eligible_groups(s) == []  # с капчей рекламу не предлагаем
        g = await s.get(Group, gid)
        assert g.ad_closed_at is not None
        g.captcha_checked_at = None
        await s.commit()
    assert await captchas.check_one(SimpleNamespace(clients={1: Client(restricted=False)}), gid) == "passed"
    async with maker() as s:
        assert [g.id for g in await adposts.eligible_groups(s)] == [gid]  # прошли — снова доступна


async def test_list_buttons(maker, monkeypatch):
    from app.bot.handlers import groups as groups_h

    gid = await _group(maker, captcha_since=now(), captcha_link="https://t.me/c/1/9")
    monkeypatch.setattr(groups_h, "manager", SimpleNamespace(clients={1: Client()}))
    cb = MagicMock()
    cb.data = "gr:cap"
    cb.answer = AsyncMock()
    cb.message.answer = AsyncMock()
    await groups_h.captcha_list(cb)
    card = cb.message.answer.call_args
    buttons = [b for row in card.kwargs.get("reply_markup", card.args[1] if len(card.args) > 1 else None).inline_keyboard
               for b in row]
    assert any(b.url == "https://t.me/c/1/9" for b in buttons)
    assert {f"gr:capok:{gid}", f"gr:capwait:{gid}", f"gr:capout:{gid}"} <= {b.callback_data for b in buttons}
    cb.data = f"gr:capout:{gid}"
    await groups_h.captcha_out(cb)
    async with maker() as s:
        assert (await s.get(Group, gid)).status == "left"


def test_kick_reason_from_full_channel():
    bot = SimpleNamespace(bot=True, username="shieldy_bot")
    full = SimpleNamespace(users=[bot], full_chat=SimpleNamespace(about="Барахолка"))
    assert captchas.kick_reason(full) == "капча: бот @shieldy_bot"
    other = SimpleNamespace(users=[SimpleNamespace(bot=True, username="MissRose_bot")],
                            full_chat=SimpleNamespace(about="Барахолка города"))
    assert captchas.kick_reason(other) == ""  # Rose капчу не всегда ставит — не пропускаем
    assert captchas.kick_reason(SimpleNamespace(users=[], full_chat=SimpleNamespace(about="Вход через капчу"))) != ""


async def test_kick_captcha_goes_to_manual_join(maker, tgbot, monkeypatch):
    """Группу с ботом-капчей не теряем: разведчик не вступает, группа — в «🖐 Вступить вручную»."""
    from app.bot.handlers import groups as groups_h
    from app.scout import client as scout_client, joiner

    monkeypatch.setattr(scout_client, "ensure_connected", AsyncMock(return_value=True))

    class C(Client):
        joined = False

        async def __call__(self, req):
            if type(req).__name__ == "JoinChannelRequest":
                self.joined = True
            return SimpleNamespace(users=[SimpleNamespace(bot=True, username="join_captcha_bot")],
                                   full_chat=SimpleNamespace(about=""))

    async with maker() as s:
        s.add(ScoutAccount(id=1, name="sc", api_id=1, api_hash="x", daily_join_limit=10))
        await s.flush()
        s.add(Group(tg_id=-931, username="bar931", title="Барахолка", status="approved", members=500))
        await s.commit()
    client = C()
    assert await joiner.join_one(client, 1) is True and not client.joined
    async with maker() as s:
        g = await s.scalar(select(Group).where(Group.tg_id == -931))
    assert g.status == "manual" and g.skip_reason.startswith("капча") and g.account_id == 1
    assert any("с телефона разведчика" in t for t in tgbot.texts(1))
    # владелец вступил с телефона разведчика → «✅ Вступил»
    monkeypatch.setattr(groups_h, "manager", SimpleNamespace(clients={1: Client(restricted=False)}))
    cb = MagicMock()
    cb.data, cb.answer = f"gr:mok:{g.id}", AsyncMock()
    cb.message.delete = AsyncMock()
    await groups_h.manual_ok(cb)
    async with maker() as s:
        g = await s.get(Group, g.id)
    assert g.status == "joined" and g.captcha_since is None


async def test_stays_when_leads_come(maker):
    from app.services import settings

    async with maker() as s:
        await settings.put(s, "captcha_leave_days", "3")
    gid = await _group(maker, captcha_since=now() - timedelta(days=4), last_lead_at=now() - timedelta(days=1))
    client = Client()
    assert await captchas.check_one(SimpleNamespace(clients={1: client}), gid) == "still"
    assert "LeaveChannelRequest" not in client.calls


async def test_admin_text_and_setting(maker):
    from app.services import settings

    assert settings.validate("mod_bot_username", "@my_moder_bot") == "my_moder_bot"
    assert settings.validate("mod_bot_username", "-") == ""
    async with maker() as s:
        await settings.put(s, "mod_bot_username", "my_moder_bot")
        text = await captchas.admin_text(s, Group(title="Барахолка Махачкала"))
    assert "@my_moder_bot" in text and len(text) <= 256
