"""Основные ниши: новые заявки попадают в свою нишу; на работающей базе ниши добавляются, удалённые админом не возвращаются."""
import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import Base, Category, Keyword
from app.seed import TREE, add_missing, seed
from app.services import rules
from app.services.text import match


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    rules.invalidate()
    async with async_sessionmaker(engine, expire_on_commit=False)() as s:
        yield s


@pytest.mark.parametrize("text, niche", [
    ("Нужен сантехник, течёт кран", "Ремонт и строительство"),
    ("Ищу грузчиков на переезд в субботу", "Перевозки и доставка"),
    ("Нужна генеральная уборка квартиры", "Клининг"),
    ("Нужен юрист по разводу", "Юристы"),
    ("Ищу бухгалтера для ИП на УСН", "Бухгалтерия"),
    ("Ищу репетитора по математике, ЕГЭ", "Репетиторы"),
    ("Нужен менеджер маркетплейса на Wildberries", "Маркетплейсы"),
    ("Ищу фотографа на свадьбу", "Фото и видео"),
    ("Нужен таргетолог для салона", "Таргет и контекст"),
    ("Нужен мастер маникюра на дом", "Красота"),
    ("Подскажите кто автоэлектрик хороший", "Авто"),
    ("Хочу заказать торт на день рождения", "Еда на заказ"),
    ("Нужен сайт для магазина", "Сайты"),
    ("Нужно пробурить скважину на участке", "Отопление и вода"),
    ("Нужен экскаватор на два дня", "Спецтехника и аренда авто"),
    ("Ищу риелтора, хочу продать квартиру", "Риелторы"),
    ("Кто может поставить пластиковые окна?", "Окна и двери"),
    ("Нужна вывеска для магазина", "Полиграфия и вывески"),
    ("Посоветуйте хорошего стоматолога", "Медицина"),
    ("Нужен муж на час повесить полку", "Мастер на час"),
    ("Ищу поставщика упаковки, нужно оптом", "Поставщики и опт"),
    ("Нужна химчистка салона машины", "Автомойка и детейлинг"),
    ("Нужны натяжные потолки в квартиру", "Потолки и полы"),
    ("Ищу запчасть на камри", "Автозапчасти"),
    ("Хочу поехать на умру в ноябре, подскажите кто организует", "Хадж и умра"),
    ("Нужно перекрыть крышу в частном доме", "Кровля и фасад"),
    ("Нужно установить видеонаблюдение в магазине", "Видеонаблюдение и охрана"),
    ("Кто может настроить роутер, не работает вайфай", "Компьютерная помощь"),
    ("Ищу частный садик для ребёнка 3 года", "Детские сады и кружки"),
    ("Нужен банкетный зал на 100 человек", "Кафе и залы"),
    ("Ищу сено для скота, нужно 50 тюков", "Сельское хозяйство"),
])
async def test_new_niches_catch_requests(session, text, niche):
    await seed(session)
    m = match(text, await rules.load_rules(session))
    assert m and m.category_name.split(" → ")[0] == niche


async def test_money_transfer_is_not_translation(session):
    await seed(session)
    assert match("Нужно сделать перевод на карту", await rules.load_rules(session)) is None


async def test_add_missing_on_old_base_and_keeps_deleted(session):
    old = TREE[:8]
    session.add(Category(name=old[0][1], emoji=old[0][0]))  # «старая» база: одна ниша, без отметки
    await session.commit()
    added = await add_missing(session)
    assert "Клининг" in added and old[0][1] not in added
    roots = (await session.scalars(select(Category.name).where(Category.parent_id.is_(None)))).all()
    assert len(roots) == len(TREE)
    # админ удалил «Клининг» — при следующем запуске он не возвращается
    cid = await session.scalar(select(Category.id).where(Category.name == "Клининг"))
    await session.execute(delete(Keyword).where(Keyword.category_id == cid))
    await session.execute(delete(Category).where((Category.id == cid) | (Category.parent_id == cid)))
    await session.commit()
    assert await add_missing(session) == []
    assert not await session.scalar(select(Category.id).where(Category.name == "Клининг"))
