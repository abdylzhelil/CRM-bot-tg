"""Память сервера и самого бота (для «Состояние бота»). Читает /proc и cgroup; где их нет (не Linux) — возвращает None."""
from pathlib import Path

WARN_AVAILABLE_MB = 200   # меньше — стоит присмотреться
BAD_AVAILABLE_MB = 150    # меньше — пора увеличивать память сервера
BAD_SWAP_USED_MB = 1024   # больше — сервер живёт на подкачке


def _meminfo(text: str) -> dict[str, int]:
    out = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if parts and parts[0].isdigit():
            out[key.strip()] = int(parts[0])  # в КБ
    return out


def read(meminfo_path: str = "/proc/meminfo", cgroup_paths: tuple[str, ...] = (
        "/sys/fs/cgroup/memory.current", "/sys/fs/cgroup/memory/memory.usage_in_bytes")) -> dict | None:
    try:
        info = _meminfo(Path(meminfo_path).read_text())
        total, avail = info["MemTotal"] // 1024, info["MemAvailable"] // 1024
    except (OSError, KeyError):
        return None
    swap_total = info.get("SwapTotal", 0) // 1024
    swap_used = max(info.get("SwapTotal", 0) - info.get("SwapFree", 0), 0) // 1024
    bot = None
    for path in cgroup_paths:
        try:
            bot = int(Path(path).read_text().strip()) // (1024 * 1024)
            break
        except (OSError, ValueError):
            continue
    level = "bad" if avail < BAD_AVAILABLE_MB or swap_used > BAD_SWAP_USED_MB else ("warn" if avail < WARN_AVAILABLE_MB else "ok")
    return {"total_mb": total, "available_mb": avail, "swap_total_mb": swap_total, "swap_used_mb": swap_used,
            "bot_mb": bot, "level": level}


def line(m: dict) -> str:
    icon = {"ok": "✅", "warn": "⚠️", "bad": "🔴"}[m["level"]]
    bot = f", бот {m['bot_mb']} МБ" if m.get("bot_mb") is not None else ""
    return f"{icon} Память: свободно {m['available_mb']} из {m['total_mb']} МБ, подкачка занята {m['swap_used_mb']} МБ{bot}"
