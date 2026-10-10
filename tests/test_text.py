from app.services.text import NodeRules, Rules, compile_phrases, match


def rules() -> Rules:
    return Rules(
        intents=compile_phrases(["ищу", "нужен", "кто сделает", "сколько стоит"]),
        negatives=compile_phrases(["делаю", "предлагаю", "вакансия"]),
        nodes=[
            NodeRules(1, None, 0, "Сайты", words=compile_phrases(["сайт", "лендинг"]),
                      strong=compile_phrases(["сделать сайт"])),
            NodeRules(2, None, 0, "Боты", words=compile_phrases(["бот", "телеграм-бот"])),
            NodeRules(3, None, 0, "Недвижимость", city=compile_phrases(["Махачкала"])[0],
                      words=compile_phrases(["помещение"])),
        ],
    )


def test_morphology():
    m = match("Ищу разработчика для сайта, срочно", rules())
    assert m and m.category_id == 1
    assert match("нужен лендинг для салона", rules()).category_id == 1
    assert match("кто сделает телеграм-бота?", rules()).category_id == 2


def test_requires_intent_unless_strong():
    assert match("красивый сайт у вас", rules()) is None
    assert match("надо сделать сайт", rules()).category_id == 1


def test_negative_words():
    assert match("Делаю сайты недорого, ищу клиентов", rules()) is None
    assert match("вакансия: нужен бот", rules()) is None


def test_city_filter():
    assert match("сниму помещение под магазин", rules()) is None
    assert match("ищу помещение в Махачкале", rules()).category_id == 3
    assert match("ищу помещение", rules(), context="Бизнес Махачкала").category_id == 3
