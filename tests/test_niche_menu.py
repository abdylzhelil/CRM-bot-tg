"""Меню ниш: разделы, поиск по слову, подсказка похожей ниши, раздел у админа, разделы в каталоге и кабинете."""
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy import select

from app.bot.handlers import catalog as catalog_h
from app.bot.handlers import categories as categories_h
from app.bot.handlers import inputs
from app.bot.handlers import profile as profile_h
from app.models import Category, Group, User, UserCategory
from app.services import niches, profile_edit
from tests.conftest import ADMIN_ID, make_user


def cbq(data, uid):
    c = MagicMock()
    c.data = data
    c.from_user.id = uid
    c.answer, c.message.answer = AsyncMock(), AsyncMock()
    return c


def msg(uid):
    m = MagicMock()
    m.from_user.id = uid
    m.answer = AsyncMock()
    return m


def buttons(markup):
    return [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row]


async def test_sections_and_toggle(maker, monkeypatch):
    sent = AsyncMock()
    monkeypatch.setattr(profile_h, "reply", sent)
    u = await make_user(maker, 50)
    await profile_h.show_niches(cbq("ob:secs", 50), u)
    text, markup = sent.call_args.args[1], sent.call_args.args[2]
    assert "Выбрано: Сайты" in text
    btns = buttons(markup)
    assert ("💻 Онлайн и IT · ✅1", "ob:sec:0") in btns and any(d == "ob:find" for _, d in btns)
    assert len(btns) < 16  # разделы, а не 45 ниш подряд
    # раздел «Дом и ремонт» → отметить «Клининг»
    async with maker() as s:
        groups = await niches.grouped(s)
        i = next(i for i, (t, _) in enumerate(groups) if t.startswith("🏠"))
        cid = await s.scalar(select(Category.id).where(Category.name == "Клининг"))
    await profile_h.open_section(cbq(f"ob:sec:{i}", 50), u)
    assert ("⬜ 🧹 Клининг", f"ob:cat:{cid}") in buttons(sent.call_args.args[2])
    await profile_h.toggle_niche(cbq(f"ob:cat:{cid}", 50), u)
    assert ("✅ 🧹 Клининг", f"ob:cat:{cid}") in buttons(sent.call_args.args[2])  # остались в том же разделе


async def test_find_by_word(maker, monkeypatch):
    sent = AsyncMock()
    monkeypatch.setattr(profile_h, "reply", sent)
    await make_user(maker, 51)
    async with maker() as s:
        await inputs.niche_find(s, msg(51), {}, "сантехник")
    assert "Ремонт и строительство" in " ".join(t for t, _ in buttons(sent.call_args.args[2]))
    async with maker() as s:
        await inputs.niche_find(s, msg(51), {}, "абракадабра")
    assert "не нашлось" in sent.call_args.args[1]
    assert any(d == "ob:propose" for _, d in buttons(sent.call_args.args[2]))


async def test_propose_suggests_existing_then_anyway(maker, tgbot):
    await make_user(maker, 52)
    m = msg(52)
    async with maker() as s:
        await inputs.niche_propose(s, m, {}, "Сантехника: сантехник, трубы, смеситель")
    assert "уже есть" in m.answer.call_args.args[0]
    btns = buttons(m.answer.call_args.kwargs["reply_markup"])
    assert any("Ремонт и строительство" in t for t, _ in btns) and ("➕ Моей нет — всё равно предложить", "ob:propforce") in btns
    async with maker() as s:
        assert not await s.scalar(select(Category).where(Category.name == "Сантехника"))
    async with maker() as s:
        user = await s.get(User, 52)
    c = cbq("ob:propforce", 52)
    await profile_h.propose_anyway(c, user)
    async with maker() as s:
        assert (await s.scalar(select(Category).where(Category.name == "Сантехника"))).pending
    assert any("Новая ниша" in t for t in tgbot.texts(ADMIN_ID))


async def test_admin_moves_niche_to_section(maker, monkeypatch):
    sent = AsyncMock()
    monkeypatch.setattr(categories_h, "reply", sent)
    async with maker() as s:
        cid = await s.scalar(select(Category.id).where(Category.name == "Клининг"))
        names = await niches.all_sections(s)
    await categories_h.open_cat(cbq(f"cat:open:{cid}", ADMIN_ID))
    assert "Раздел в списке ниш: 🏠 Дом и ремонт" in sent.call_args.args[1]
    assert ("📂 Раздел", f"cat:sec:{cid}") in buttons(sent.call_args.args[2])
    other = names.index("📌 Другое")
    await categories_h.set_section(cbq(f"cat:secset:{cid}:{other}", ADMIN_ID))
    async with maker() as s:
        assert (await s.get(Category, cid)).section == "📌 Другое"
        assert [t for t, _ in await niches.grouped(s)][-1] == "📌 Другое"


async def test_catalog_picks_section_then_niche(maker, monkeypatch):
    sent = AsyncMock()
    monkeypatch.setattr(catalog_h, "reply", sent)
    u = await make_user(maker, 53)
    async with maker() as s:
        g = Group(tg_id=-100777, title="Чат", username="chat", status="draft", submitted_by=53)
        s.add(g)
        await s.commit()
        rows = await catalog_h._section_rows(s, g.id)
    assert all(d.startswith(f"cg:sec:{g.id}:") for row in rows for _, d in row)
    await catalog_h.pick_section(cbq(f"cg:sec:{g.id}:0", 53), u)
    assert any(d.startswith(f"cg:cat:{g.id}:") for _, d in buttons(sent.call_args.args[2]))


async def test_cabinet_niches_have_sections(maker):
    u = await make_user(maker, 54)
    async with maker() as s:
        p = await profile_edit.get_profile(s, u)
        assert p["niches"][0]["section"] == "💻 Онлайн и IT"
        assert {n["section"] for n in p["niches"]} >= {"🏠 Дом и ремонт", "💼 Бизнес-услуги"}
        assert await s.get(UserCategory, (54, p["niches"][0]["id"]))  # «Сайты» отмечены


async def test_typed_city_typo_suggests_known(maker, tgbot):
    """Город с опечаткой не сохраняется молча (иначе заявки почти не приходят): подсказка и выбор."""
    from unittest.mock import AsyncMock, MagicMock

    from app.bot.handlers import inputs, profile
    from app.models import User
    from app.services import geo

    async with maker() as s:
        s.add(User(tg_id=7070, name="X", onboarded=True))
        await s.commit()
    m = MagicMock()
    m.from_user.id = 7070
    m.answer = AsyncMock()
    async with maker() as s:
        await inputs.city_other(s, m, {}, "Махачкла")
    text, markup = m.answer.call_args.args[0], m.answer.call_args.kwargs["reply_markup"]
    assert "Махачкала" in text
    data = [b.callback_data for row in markup.inline_keyboard for b in row]
    i = geo.KNOWN_PLACES.index("Махачкала")
    assert f"ob:cityx:{i}" in data and "ob:citykeep" in data
    async with maker() as s:
        assert (await s.get(User, 7070)).city == ""  # пока не выбрал — не сохранили
        await inputs.city_other(s, m, {}, "в Махачкале")  # узнаваемый — сразу
        await s.commit()
    async with maker() as s:
        assert (await s.get(User, 7070)).city == "Махачкала"
    assert profile.PENDING_CITY.get(7070) == "Махачкла"
