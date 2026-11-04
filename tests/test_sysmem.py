"""Память сервера в «Состоянии бота»."""
from app.services import sysmem

SAMPLE = """MemTotal:         984064 kB
MemFree:           91000 kB
MemAvailable:     {avail} kB
SwapTotal:       2096128 kB
SwapFree:        {swapfree} kB
"""


def write(tmp_path, avail_mb, swap_used_mb=300, cgroup_mb=265):
    mi = tmp_path / "meminfo"
    mi.write_text(SAMPLE.format(avail=avail_mb * 1024, swapfree=(2047 - swap_used_mb) * 1024))
    cg = tmp_path / "memory.current"
    cg.write_text(str(cgroup_mb * 1024 * 1024))
    return str(mi), (str(cg),)


def test_levels_and_values(tmp_path):
    m = sysmem.read(*write(tmp_path, 314))
    assert m["total_mb"] == 961 and m["available_mb"] == 314 and m["swap_used_mb"] == 300 and m["bot_mb"] == 265 and m["level"] == "ok"
    assert sysmem.read(*write(tmp_path, 180))["level"] == "warn"
    assert sysmem.read(*write(tmp_path, 120))["level"] == "bad"
    assert sysmem.read(*write(tmp_path, 400, swap_used_mb=1500))["level"] == "bad"  # живёт на подкачке


def test_missing_files_do_not_break(tmp_path):
    assert sysmem.read(str(tmp_path / "нет"), ()) is None
    mi, _ = write(tmp_path, 300)
    m = sysmem.read(mi, (str(tmp_path / "нет"),))
    assert m["bot_mb"] is None and "бот" not in sysmem.line(m)


def test_line_text(tmp_path):
    line = sysmem.line(sysmem.read(*write(tmp_path, 314)))
    assert line.startswith("✅ Память: свободно 314 из 961 МБ") and "бот 265 МБ" in line
    assert sysmem.line(sysmem.read(*write(tmp_path, 100))).startswith("🔴")


async def test_memory_in_status_text_checklist_and_api(maker, monkeypatch, tmp_path):
    from app.bot.handlers.settings import status_text
    from app.db import Session
    from app.services import autosetup

    low = sysmem.read(*write(tmp_path, 120))
    monkeypatch.setattr(sysmem, "read", lambda *a, **k: low)
    assert "🔴 Память" in await status_text()
    async with Session() as s:
        ok, bad = await autosetup.admin_checklist(s)
    assert any("Память" in x and "увеличить" in x for x in bad)
    monkeypatch.setattr(sysmem, "read", lambda *a, **k: None)  # не Linux: строки просто нет
    assert "Память" not in await status_text()
