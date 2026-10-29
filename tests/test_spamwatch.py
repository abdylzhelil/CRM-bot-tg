"""🕵️ Сторож спама и 🔄 «Начать заново» с группами разведчика."""
from datetime import timedelta

from sqlalchemy import select

from app.models import AdOrder, AdPost, Group, Lead, ScoutAccount, now
from app.services import groupcheck, settings, spamwatch, tidy

JUNK = "Делаю сайты под ключ, примеры работ в профиле, пишите в лс, цены от 5000 ₽"
OK = "Подскажите пожалуйста кто знает хорошего сантехника в Махачкале срочно"


def test_is_junk():
    assert spamwatch.is_junk(JUNK) is True
    assert spamwatch.is_junk("Earn money fast with our crypto bot today friends") is True
    assert spamwatch.is_junk(OK) is False
    assert spamwatch.is_junk("привет всем") is None


async def _scout(s):
    acc = ScoutAccount(name="sc", api_id=1, api_hash="x", phone="+70000000001", status="ok")
    s.add(acc)
    await s.flush()
    return acc.id


async def test_spam_group_found_but_lead_group_kept(maker):
    async with maker() as s:
        acc = await _scout(s)
        s.add_all([Group(tg_id=-1001, title="Махачкала объявления", status="joined", account_id=acc),
                   Group(tg_id=-1002, title="Дагестан чат", status="joined", account_id=acc,
                         last_lead_at=now() - timedelta(days=2)),
                   Group(tg_id=-1003, title="Каспийск", status="joined", account_id=acc)])
        await s.commit()
    for _ in range(45):
        spamwatch.note(-1001, JUNK)
        spamwatch.note(-1002, JUNK)
        spamwatch.note(-1003, OK)
    async with maker() as s:
        assert await spamwatch.flush(s) == 3
        bad = await spamwatch.suspects(s)
    assert [g.tg_id for g in bad] == [-1001]  # у -1002 недавно была заявка, -1003 чистая
    assert "сторож" in spamwatch.verdict(bad[0])


async def test_window_halves_counts(maker):
    async with maker() as s:
        s.add(Group(tg_id=-1010, title="x", status="joined", watch_seen=spamwatch.WINDOW, watch_junk=spamwatch.WINDOW))
        await s.commit()
    for _ in range(10):
        spamwatch.note(-1010, OK)
    async with maker() as s:
        await spamwatch.flush(s)
        g = await s.scalar(select(Group).where(Group.tg_id == -1010))
    assert g.watch_seen == (spamwatch.WINDOW + 10) // 2 and g.watch_junk == spamwatch.WINDOW // 2


async def test_quiet_groups_rechecked_weekly(maker):
    async with maker() as s:
        acc = await _scout(s)
        s.add_all([Group(tg_id=-1020, title="a", status="joined", account_id=acc, audit_at=now() - timedelta(days=8)),
                   Group(tg_id=-1021, title="b", status="joined", account_id=acc, audit_at=now() - timedelta(days=8),
                         leads_count=3)])
        await s.commit()
        due = await groupcheck.due(s, {acc}, limit=10)
        names = [(await s.get(Group, i)).tg_id for i in due]
    assert names == [-1020]


async def test_reset_plan_and_finish(maker):
    async with maker() as s:
        acc = await _scout(s)
        groups = [
            Group(tg_id=-2001, title="Махачкала услуги", status="joined", account_id=acc),        # выйти и удалить
            Group(tg_id=-2002, title="Взаимопиар флуд", status="joined", account_id=acc),        # выйти и запомнить
            Group(tg_id=-2003, title="Очередь", status="approved"),                               # удалить
            Group(tg_id=-2004, title="Каталог", status="joined", account_id=acc, catalog=True),   # не трогать
            Group(tg_id=-2005, title="Бот", status="joined", bot_member=True),                    # не трогать
            Group(tg_id=-2006, title="Реклама была", status="left"),                              # есть пост — не трогать
            Group(tg_id=-2007, title="Пропущена", status="skipped", skip_reason="проверка: заработок"),  # запомнить
        ]
        s.add_all(groups)
        await s.flush()
        order = AdOrder(user_id=1, text="t")
        s.add(order)
        await s.flush()
        s.add(AdPost(order_id=order.id, group_id=groups[5].id))
        s.add(Lead(author_id=5, text="ищу мастера", text_hash="h", group_id=groups[0].id))
        await settings.put(s, "search_offset", "40")
        await settings.put(s, "scout_grow", "0")
        await s.commit()
        p = await tidy.reset_plan(s)
        ids = {g.tg_id: g.id for g in groups}
    assert sorted(p["leave"]) == sorted([ids[-2001], ids[-2002]])
    assert p["keep"] == 3 and p["delete"] == 2 and sorted(p["spam"]) == sorted([ids[-2002], ids[-2007]])
    async with maker() as s:  # будто разведчик вышел из обеих
        for gid in p["leave"]:
            (await s.get(Group, gid)).status = "left"
        await s.commit()
        res = await tidy.reset_finish(s, set(p["spam"]))
        left = {g.tg_id: g.status for g in (await s.scalars(select(Group))).all()}
        lead = await s.scalar(select(Lead))
        offset, grow = await settings.get(s, "search_offset"), await settings.get(s, "scout_grow")
    assert res == {"deleted": 2, "remembered": 2, "still": 0}
    assert left == {-2002: "skipped", -2004: "joined", -2005: "joined", -2006: "left", -2007: "skipped"}
    assert lead.group_id is None and offset == "0" and grow == "1"


def test_ai_query_ideas_are_cleaned():
    from app.services import queryideas

    got = queryideas.clean(["барахолка Кизилюрт", "Взаимопиар Дагестан", "объявления Карабудахкентский район",
                            "Барахолка кизилюрт", "https://t.me/x", "очень длинный запрос из многих разных слов", 5])
    assert got == ["барахолка Кизилюрт", "объявления Карабудахкентский район"]


async def test_ai_ideas_join_their_circle(maker, monkeypatch):
    from app.scout import search
    from app.services import brain, queryideas

    answers = iter([{"queries": ["барахолка Кизилюрт"]}, {"queries": ["объявления Назрань район"]},
                    {"queries": ["услуги Тула"]}])
    monkeypatch.setattr(brain, "ask", lambda *a, **k: _ret(next(answers)))
    async with maker() as s:
        assert await queryideas.refresh(s, [["Дагестан"], ["Чечня"], ["Тула"]], ["сантехник"]) == 3
        assert await queryideas.refresh(s, [["Дагестан"], ["Чечня"], ["Тула"]], ["сантехник"]) == 0  # раз в неделю
        home_q, near_q, rest = await search.tier_queries(s)
    assert "барахолка Кизилюрт" in home_q and "объявления Назрань район" in near_q and "услуги Тула" in rest


async def _ret(x):
    return x


async def test_doubtful_groups_go_to_ai_check(maker):
    async with maker() as s:
        acc = await _scout(s)
        s.add_all([Group(tg_id=-3001, title="a", status="joined", account_id=acc, watch_seen=100, watch_junk=55),
                   Group(tg_id=-3002, title="b", status="joined", account_id=acc, watch_seen=100, watch_junk=20),
                   Group(tg_id=-3003, title="c", status="joined", account_id=acc, watch_seen=100, watch_junk=55,
                         audit_at=now() - timedelta(days=1))])
        await s.commit()
        ids = await spamwatch.doubtful(s)
        assert [(await s.get(Group, i)).tg_id for i in ids] == [-3001]
