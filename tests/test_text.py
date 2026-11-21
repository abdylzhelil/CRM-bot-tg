import pytest

from app.services.text import NodeRules, Rules, compile_negatives, compile_phrases, match


def rules() -> Rules:
    return Rules(
        intents=compile_phrases(["ищу", "нужен", "кто сделает", "сколько стоит"]),
        negatives=compile_negatives(["делаю", "предлагаю", "вакансия"]),
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


def test_client_asking_who_does_is_not_negative():
    assert match("Подскажите, кто делает сайты? Нужен лендинг", rules()).category_id == 1
    assert match("Делаю сайты недорого, ищу клиентов", rules()) is None


@pytest.mark.parametrize("text", [
    "Ищу мастера снять обои в квартире",
    "Нужна помощь снять доски с навеса",
    "Нужен помощник собрать стол в квартире",
    "Нужно снять ролик о помещении",
    "Ищу человека разгрузить мебель в помещение",
])
def test_work_at_property_is_not_property_search(text):
    r = Rules(intents=compile_phrases(["ищу", "нужен", "нужно"]), nodes=[
        NodeRules(1, None, 0, "Недвижимость", words=compile_phrases(["сниму", "квартира", "помещение"])),
    ])
    assert match(text, r) is None


@pytest.mark.parametrize("text", [
    "Ищу помещение", "Ищу небольшое помещение", "Сниму квартиру",
    "Квартиру сниму", "Нужна квартира", "Ищу помещение в аренду",
])
def test_actual_property_search_survives(text):
    r = Rules(intents=compile_phrases(["ищу", "нужен", "сниму"]), nodes=[
        NodeRules(1, None, 0, "Недвижимость", words=compile_phrases(["квартира", "помещение"])),
    ])
    assert match(text, r).category_id == 1


def test_service_category_can_win_after_property_false_hit_removed():
    r = Rules(intents=compile_phrases(["нужен"]), nodes=[
        NodeRules(1, None, 0, "Недвижимость", words=compile_phrases(["сниму", "квартира"])),
        NodeRules(2, None, 0, "Ремонт и строительство", words=compile_phrases(["мастер"])),
    ])
    assert match("Нужен мастер снять обои в квартире", r).category_id == 2
