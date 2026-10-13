from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot.common import Ask, ask, esc, kb, reply
from app.db import Session
from app.services import admin_status, autosetup, settings, sysmem

router = Router()


@router.message(F.text == "⚙️ Настройки")
async def settings_menu(target: Message | CallbackQuery) -> None:
    """Главная страница: разделы по два в ряд, чтобы не было длинной простыни."""
    buttons = [(f"{icon} {title}", f"st:sec:{i}") for i, (icon, title, _) in enumerate(settings.SECTIONS)]
    rows = [[("⚡ Автонастройка бота", "st:auto")], [("📋 Состояние бота", "st:status")], [("🎛 Пресеты", "st:presets"), ("🔎 Найти настройку", "st:find")]]
    rows += [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    rows.append([("👤 Аккаунты-разведчики", "acc:list")])
    rows.append([("📢 Статистику в канал", "pub:now")])
    await reply(target, "⚙️ <b>Настройки</b>\nВыберите раздел:", kb(rows))


@router.callback_query(F.data == "st:menu")
async def back(cb: CallbackQuery) -> None:
    await settings_menu(cb)


@router.callback_query(F.data.regexp(r"^st:sec:\d+$"))
async def section(cb: CallbackQuery) -> None:
    icon, title, keys = settings.SECTIONS[int(cb.data.split(":")[2])]
    rows = []
    async with Session() as s:
        for key in keys:
            value = await settings.get(s, key) or "выкл"
            rows.append([(f"{settings.SHORT.get(key, settings.LABELS[key])}: {value}", f"st:{key}")])
    idx = cb.data.split(":")[2]
    rows.append([("◀️ Все настройки", "st:menu")])
    text = f"{icon} <b>{esc(title)}</b>\nНажмите на пункт, чтобы изменить."
    if idx == "0":
        text += "\n\nЦена 0 — пакет не продаётся и кнопка не показывается."
    await reply(cb, text, kb(rows))


def _flag(ok: bool) -> str:
    return "✅" if ok else "⬜"


async def status_text() -> str:
    """Сводка «что включено и как идёт»: чтобы за один экран понять, в каком состоянии бот."""
    async with Session() as s:
        d = await admin_status.collect(s)
    accounts = ", ".join(f"{st} {n}" for st, n in d["accounts"].items()) or "нет"
    refund = d["refund_days"]
    return "\n".join([
        "📋 <b>Состояние бота</b>",
        f"👥 Зарегистрировано: {d['users']}, на 💎: {d['paid']}",
        f"📥 За сутки: заявок {d['leads_day']}, рассылок {d['sent_day']}; оплат за 30 дней: {d['payments_30d']}",
        "",
        f"🔍 Разведчик: {'⏸ на паузе' if d['scout_paused'] else '▶️ работает'}, режим {esc(d['mode'])}; аккаунты: {esc(accounts)}",
        f"{_flag(d['selling'])} Продажа 💎: " + (" / ".join(f"{p}₽" for p in d["prices"]) if d["selling"] else "не продаётся (цены 0)"),
        f"{_flag(d['pay_ready'])} Ключи оплаты в .env: {'есть' if d['pay_ready'] else 'нет'}",
        f"{_flag(refund != '0')} Возврат: {refund} дн." if refund != "0" else f"{_flag(False)} Возврат выключен",
        f"{_flag(d['autorenew'])} Автопродление   {_flag(d['license'])} Лицензия   {_flag(d['ads'])} Реклама групп",
        f"{_flag(d['ai'])} Claude (платно)"
        + ("" if d["ai_key"] or not d["ai"] else " — ⚠️ нет ANTHROPIC_API_KEY"),
        f"{_flag(bool(d['stats_channel']))} Канал статистики: {esc(d['stats_channel']) or 'не подключён'}",
    ] + ([sysmem.line(d["memory"])] if d["memory"] else []))


@router.callback_query(F.data == "st:status")
async def status(cb: CallbackQuery) -> None:
    await reply(cb, await status_text(), kb([[("🔄 Обновить", "st:status")], [("◀️ Все настройки", "st:menu")]]))


@router.callback_query(F.data == "st:auto")
async def auto(cb: CallbackQuery) -> None:
    """Автонастройка бота: проверка готовности + быстрый путь к пресету «Запуск»; ничего не меняется без вашего подтверждения."""
    async with Session() as s:
        ok, bad = await autosetup.admin_checklist(s)
        diff = await settings.preset_diff(s, "launch")
    text = "⚡ <b>Автонастройка бота</b>\n"
    if bad:
        text += "\n<b>Исправить:</b>\n" + "\n".join(f"⚠️ {esc(x)}" for x in bad)
    text += "\n\n<b>В порядке:</b>\n" + "\n".join(f"✅ {esc(x)}" for x in ok)
    rows = []
    if diff:
        text += f"\n\n🚀 Рекомендуемые значения для запуска отличаются в {len(diff)} настройках — посмотрите и примените."
        rows.append([("🚀 Показать пресет «Запуск»", "st:pre:launch")])
    rows += [[("🙋 Моя автонастройка", "pf:auto")], [("◀️ Все настройки", "st:menu")]]
    await reply(cb, text, kb(rows))


@router.callback_query(F.data == "st:presets")
async def presets(cb: CallbackQuery) -> None:
    rows = [[(f"{icon} {title}", f"st:pre:{name}")] for name, (icon, title, _, _) in settings.PRESETS.items()]
    rows.append([("◀️ Все настройки", "st:menu")])
    text = "🎛 <b>Пресеты</b> — набор значений одним нажатием. Перед применением покажу, что изменится.\n\n"
    text += "\n".join(f"{icon} <b>{title}</b> — {desc}" for icon, title, desc, _ in settings.PRESETS.values())
    await reply(cb, text, kb(rows))


@router.callback_query(F.data.regexp(r"^st:pre:\w+$"))
async def preset_preview(cb: CallbackQuery) -> None:
    name = cb.data.split(":")[2]
    if name not in settings.PRESETS:
        return await cb.answer()
    icon, title, _, _ = settings.PRESETS[name]
    async with Session() as s:
        diff = await settings.preset_diff(s, name)
    if not diff:
        return await reply(cb, f"{icon} <b>{title}</b>: всё уже так выставлено.", kb([[("◀️ Пресеты", "st:presets")]]))
    lines = [f"• {esc(settings.SHORT.get(k, settings.LABELS[k]))}: {esc(old) or '—'} → <b>{esc(new)}</b>" for k, old, new in diff]
    await reply(cb, f"{icon} <b>{title}</b> изменит:\n" + "\n".join(lines),
                kb([[("✅ Применить", f"st:preok:{name}"), ("Отмена", "st:presets")]]))


@router.callback_query(F.data.regexp(r"^st:preok:\w+$"))
async def preset_apply(cb: CallbackQuery) -> None:
    name = cb.data.split(":")[2]
    if name not in settings.PRESETS:
        return await cb.answer()
    async with Session() as s:
        n = await settings.apply_preset(s, name)
    await reply(cb, f"✅ Пресет «{settings.PRESETS[name][1]}» применён, изменено настроек: {n}.",
                kb([[("📋 Состояние бота", "st:status"), ("⚙️ К настройкам", "st:menu")]]))


@router.callback_query(F.data == "st:find")
async def find(cb: CallbackQuery, state: FSMContext) -> None:
    await ask(cb, state, "setting_find", "🔎 Что ищем? Напишите слово: например «цена», «возврат», «спам», «город».")


@router.callback_query(F.data.regexp(r"^st:def:\w+$"))
async def reset_default(cb: CallbackQuery, state: FSMContext) -> None:
    key = cb.data.split(":")[2]
    if key not in settings.LABELS:
        return await cb.answer()
    async with Session() as s:
        await settings.put(s, key, settings.DEFAULTS[key])
    await state.clear()
    await cb.message.answer(f"↩️ {esc(settings.LABELS[key])}: по умолчанию ({esc(settings.DEFAULTS[key]) or 'выкл'})",
                            reply_markup=kb([[("⚙️ К настройкам", "st:menu")]]))
    await cb.answer()


@router.callback_query(F.data.regexp(r"^st:\w+$"))
async def edit(cb: CallbackQuery, state: FSMContext) -> None:
    key = cb.data[3:]
    if key not in settings.LABELS:
        return await cb.answer()
    hint = {
        "mode": " (semi — присылать карточки, auto — вступать самому)",
        "quiet_hours": ": с какого по какой час копить заявки, напр. 23-8. "
        "🔥 Горячие придут сразу. «-» — выключить",
        "report_hour": " (0–23, по времени TZ_NAME из .env)",
        "channel_hour": " (0–23, по времени TZ_NAME из .env)",
    }.get(key, " (число)")
    async with Session() as s:
        cur = await settings.get(s, key)
    text = (f"<b>{esc(settings.LABELS[key])}</b>\n{esc(settings.HINTS.get(key, ''))}\n\n"
            f"Сейчас: <b>{esc(cur) or 'выкл'}</b>, по умолчанию: {esc(settings.DEFAULTS[key]) or 'выкл'}\n"
            f"Введите новое значение{hint}\n\n/cancel — отмена")
    await state.set_state(Ask.value)
    await state.update_data(action="setting", key=key)
    await cb.message.answer(text, reply_markup=kb([[("↩️ По умолчанию", f"st:def:{key}")]]))
    await cb.answer()


@router.callback_query(F.data == "pub:now")
async def publish_now(cb: CallbackQuery) -> None:
    from app.services import public_stats

    ok = await public_stats.publish(cb.bot)
    from app import config

    if ok:
        return await cb.answer("Опубликовано в канале", show_alert=True)
    if not config.STATS_CHANNEL:
        return await cb.answer(
            "Канал для статистики пока не подключён — это необязательная функция. "
            "Чтобы включить: создайте канал, добавьте бота администратором и впишите STATS_CHANNEL=@имя_канала в файл .env.",
            show_alert=True)
    await cb.answer("Не получилось написать в канал. Проверьте, что бот добавлен в него администратором.", show_alert=True)
