from app.services import templates
from app.services.quiet import in_quiet, is_hot, parse_hours


def test_parse_and_in_quiet_over_midnight():
    h = parse_hours("23-8")
    assert h == (23, 8)
    assert in_quiet(23, h) and in_quiet(3, h) and not in_quiet(8, h) and not in_quiet(15, h)
    assert parse_hours("") is None and parse_hours("-") is None and parse_hours("5-5") is None
    assert in_quiet(13, parse_hours("13-15")) and not in_quiet(15, parse_hours("13-15"))
    assert not in_quiet(3, None)


def test_is_hot():
    assert is_hot("Срочно нужен лендинг")
    assert is_hot("нужен сайт, бюджет 15к")
    assert not is_hot("подскажите кто делает сайты")


def test_template_default_and_custom():
    t = templates.render("", name="Марат Алиев", group="Бизнес МХК", category="лендинг", prices="от 10 000 ₽")
    assert t.startswith("Здравствуйте, Марат!") and "Цены: от 10 000 ₽" in t and "«Бизнес МХК»" in t
    t = templates.render("Привет{, имя}! {цены}", name="", group="", category="бот", prices="")
    assert t == "Привет!"
