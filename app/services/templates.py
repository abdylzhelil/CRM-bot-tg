"""Шаблон ответа клиенту из категории (без ИИ)."""

DEFAULT = (
    "Здравствуйте{, имя}! Увидел ваш запрос в «{группа}». Я как раз занимаюсь этим — {категория}.\n"
    "{цены}\n"
    "Расскажите немного подробнее о задаче, и я сделаю предложение под вас."
)


def render(template: str, *, name: str, group: str, category: str, prices: str) -> str:
    text = template.strip() or DEFAULT
    first = name.split()[0] if name.strip() else ""
    text = text.replace("{, имя}", f", {first}" if first else "")
    for key, value in {
        "{имя}": first,
        "{группа}": group or "группе",
        "{категория}": category,
        "{цены}": f"Цены: {prices.strip()}" if prices.strip() else "",
    }.items():
        text = text.replace(key, value)
    # убираем пустые строки, оставшиеся от пустых подстановок
    return "\n".join(line.rstrip() for line in text.splitlines() if line.strip())
