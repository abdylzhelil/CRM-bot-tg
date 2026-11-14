"""Придуманные примеры местных заказов; клиентские тексты сюда не копируются."""
import pytest

from app.seed import TREE, INTENTS, NEGATIVES
from app.services.text import NodeRules, Rules, compile_negatives, compile_phrases, match


def default_rules():
    return Rules(
        intents=compile_phrases(INTENTS), negatives=compile_negatives(NEGATIVES),
        nodes=[NodeRules(i, None, 0, name, city=compile_phrases([city])[0] if city else [],
                         words=compile_phrases(words), strong=compile_phrases(strong))
               for i, (_, name, _, words, strong, city) in enumerate(TREE, 1)],
    )


@pytest.mark.parametrize("text,niche", [
    ("Нужен помощник собрать письменный стол в квартире", "Мебель и интерьер"),
    ("Ищу мастера для сборки двух шкафов", "Мебель и интерьер"),
    ("Надо собрать кровать после переезда", "Мебель и интерьер"),
    ("Ищу мастера, кто собирает шкафы", "Мебель и интерьер"),
    ("Нужно разгрузить стройматериалы во дворе", "Перевозки и доставка"),
    ("Нужна помощь занести холодильник в помещение", "Перевозки и доставка"),
    ("Нужно перенести дрова из машины, с меня обед", "Перевозки и доставка"),
    ("Требуется разгрузка кирпича и цемента", "Перевозки и доставка"),
    ("Нужны грузчики разгрузить кирпич и цемент", "Перевозки и доставка"),
    ("Нужно разгрузить машину", "Перевозки и доставка"),
    ("Надо снять старые обои в квартире", "Вывоз мусора и демонтаж"),
    ("Ищу помощника демонтировать плитку", "Вывоз мусора и демонтаж"),
])
def test_service_action_and_object(text, niche):
    found = match(text, default_rules(), context="Махачкала")
    assert found and found.category_name == niche


@pytest.mark.parametrize("text", [
    "Нужно собрать документы для продажи шкафа",
    "Нужно снять видео про старые обои",
    "Нужно собрать команду для магазина мебели",
    "Надо перенести встречу, холодильник привезут потом",
    "Ищу помощника, с меня обед",
    "Ищу помощника, обед за мой счёт",
    "Собираем шкафы, нужны новые клиенты",
    "Разгружаем коробки, нужен напарник",
    "Нужно поднять машину домкратом",
    "Надо снять сарай на месяц",
    "Нужно собрать. Стол уже готов",
    "Нужно снять. Обои уже поклеены",
])
def test_unrelated_actions_and_meal_perks(text):
    found = match(text, default_rules(), context="Махачкала")
    assert found is None


@pytest.mark.parametrize("text,niche", [
    ("Нужно купить кирпич и цемент", "Стройматериалы"),
    ("Ищу повара приготовить обед для гостей", "Еда на заказ"),
    ("Хочу заказать обеды для рабочих", "Еда на заказ"),
    ("Нужно заказать обед для гостей, с меня обед помощникам", "Еда на заказ"),
    ("Ищу небольшую квартиру", "Недвижимость"),
])
def test_existing_goods_and_food_orders(text, niche):
    assert match(text, default_rules(), context="Махачкала").category_name == niche


def test_alias_requires_configured_keyword_intent_and_respects_exclusions():
    r = Rules(intents=compile_phrases(["нужно"]), nodes=[
        NodeRules(1, None, 0, "Перевозки и доставка", words=compile_phrases(["грузчик"])),
    ])
    assert match("Нужно разгрузить шкаф", r).category_id == 1
    assert match("Вчера разгрузили шкаф", r) is None
    r.nodes[0].city = compile_phrases(["Махачкала"])[0]
    assert match("Нужно разгрузить шкаф", r) is None
    assert match("Нужно разгрузить шкаф", r, context="Махачкала").category_id == 1
    r.nodes[0].city = []
    r.nodes[0].negatives = compile_negatives(["шкаф"])
    assert match("Нужно разгрузить шкаф", r) is None
    r.nodes[0].negatives = []
    r.negatives = compile_negatives(["разгрузить"])
    assert match("Нужно разгрузить шкаф", r) is None
    r.negatives = compile_negatives(["грузчик"])
    assert match("Нужно разгрузить шкаф", r) is None
    r.negatives = []
    r.nodes[0].words = []
    assert match("Нужно разгрузить шкаф", r) is None
    r.nodes = []
    assert match("Нужно разгрузить шкаф", r) is None


async def test_disabled_category_and_deleted_anchor_stay_disabled(maker):
    from sqlalchemy import select
    from app.models import Category, Keyword
    from app.services import rules

    async with maker() as s:
        cat = await s.scalar(select(Category).where(Category.name == "Перевозки и доставка"))
        text = "Нужно разгрузить шкаф"
        result = match(text, await rules.load_rules(s))
        assert result.category_id == cat.id and result.hits == ["грузчик"]
        cat.enabled = False
        await s.flush()
        rules.invalidate()
        assert match(text, await rules.load_rules(s)) is None
        cat.enabled = True
        anchor = await s.scalar(select(Keyword).where(Keyword.category_id == cat.id, Keyword.word == "грузчик"))
        from app.services import badwords
        await badwords.toggle(s, anchor.id)
        await s.flush()
        assert match(text, await rules.load_rules(s)) is None
        await badwords.toggle(s, anchor.id)
        await s.flush()
        assert match(text, await rules.load_rules(s)).category_id == cat.id
        await s.delete(anchor)
        await s.flush()
        rules.invalidate()
        assert match(text, await rules.load_rules(s)) is None
