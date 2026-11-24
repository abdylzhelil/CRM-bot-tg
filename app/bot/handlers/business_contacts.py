"""Private owner UI for publicly advertised business contacts in Dagestan."""
from aiogram import F, Router
from aiogram.types import CallbackQuery, BufferedInputFile

from app import config
from app.bot.common import esc, kb, reply
from app.db import Session
from app.services import business_contacts as service

router = Router()
router.callback_query.filter(F.from_user.id.in_(config.OWNER_IDS), F.message.chat.type == 'private')


async def show(cb, status='new', page=0):
    async with Session() as s:
        data = await service.load(s)
    rows = list(data['contacts'].values())
    selected = sorted((r for r in rows if r['status'] == status), key=lambda r:r['handle'])
    page = max(0, min(page, max(0,(len(selected)-1)//8)))
    text = ('📇 <b>Деловые контакты Дагестана</b>\n\n'
            'Опубликованные контакты для обращений из описаний известных боту публичных групп Дагестана. '
            'До 100 источников за поиск, до 500 контактов в списке. Это не все контакты региона. Регион источника не подтверждает место проживания человека. '
            'Активность самого контакта не проверена.\n\n'
            f'Всего: {len(rows)} · {service.STATUSES[status]}: {len(selected)}\n'
            'Отправка не выполняется автоматически. Статус «Написал» отмечаете после отправки.')
    buttons = [[('🔎 Найти контакты','dc:search'),('⏹ Остановить','dc:stop')]]
    for i in range(0,len(service.STATUSES),2):
        items=list(service.STATUSES.items())[i:i+2]
        buttons.append([(f'{label} · {sum(r["status"]==key for r in rows)}',f'dc:list:{key}:0') for key,label in items])
    buttons += [[('@'+r['handle'], 'dc:card:'+r['handle'])] for r in selected[page*8:page*8+8]]
    if page: buttons.append([('◀️ Предыдущие',f'dc:list:{status}:{page-1}')])
    if (page+1)*8<len(selected): buttons.append([('Следующие ▶️',f'dc:list:{status}:{page+1}')])
    if data.get('report'):
        r=data['report'];text+=f'\nПоследний поиск: проверено {r["checked"]}/{r["sources"]}, новых {r["added"]}, ошибок {r["errors"]}.'
    buttons += [[('📄 Скачать список CSV','dc:export')],[('◀️ Управление','dc:home')]]
    await reply(cb,text,kb(buttons))


@router.callback_query(F.data == 'dc:menu')
async def menu(cb: CallbackQuery):
    await show(cb)


@router.callback_query(F.data.startswith('dc:list:'))
async def listing(cb: CallbackQuery):
    _,_,status,page=cb.data.split(':')
    if status not in service.STATUSES or not page.isdigit():return
    await show(cb,status,int(page))


@router.callback_query(F.data == 'dc:search')
async def search(cb: CallbackQuery):
    await cb.answer('Ищу опубликованные деловые контакты')
    await cb.message.answer('🔎 Проверяю известные публичные источники Дагестана. '
                            'Остановить можно кнопкой «⏹ Остановить» в списке.')
    try:
        r=await service.search()
        await cb.message.answer(f'📇 Поиск {"остановлен" if r["stopped"] else "закончен"}: '
                                f'проверено {r["checked"]}/{r["sources"]}, новых контактов {r["added"]}, ошибок {r["errors"]}.',
                                reply_markup=kb([[('📇 Список контактов','dc:menu')]]))
    except service.ContactError as e:
        await cb.message.answer(esc(str(e)),reply_markup=kb([[('◀️ Контакты','dc:menu')]]))


@router.callback_query(F.data == 'dc:stop')
async def stop(cb: CallbackQuery):
    service.cancel();await cb.answer('Остановлю после текущего источника. Найденное сохранено.')


@router.callback_query(F.data.startswith('dc:card:'))
async def card(cb: CallbackQuery):
    handle=cb.data.split(':')[-1]
    async with Session() as s:
        row=(await service.load(s))['contacts'].get(handle)
    if not row:return await cb.answer('Контакт больше не найден')
    text=f'📇 <b>@{esc(handle)}</b>\nСтатус: {service.STATUSES[row["status"]]}\nАктивность контакта: не проверена.\n'
    for src in row['sources']:
        text+=f'\nИсточник: {esc(src["title"])}\n{esc(src["url"])}\n{esc(src["evidence"])}\nПроверено: {esc(src["checked_at"][:16])}\n'
    buttons=[] if row['status']=='no' else [[('✍️ Открыть контакт','url:https://t.me/'+handle)]]
    buttons += [[(label,f'dc:mark:{key}:{handle}') for key,label in list(service.STATUSES.items())[i:i+2]] for i in range(0,5,2)]
    buttons.append([('◀️ Список','dc:menu')])
    await reply(cb,text,kb(buttons))


@router.callback_query(F.data.startswith('dc:mark:'))
async def mark(cb: CallbackQuery):
    _,_,status,handle=cb.data.split(':')
    if status=='new':
        return await reply(cb,'Вернуть контакт в новые? Проверьте, что он не отказывался от обращений.',
                           kb([[('Да, вернуть','dc:reset:'+handle)],[('◀️ Назад','dc:card:'+handle)]]))
    await service.mark(handle,status);await card_view(cb,handle)


async def card_view(cb,handle):
    await cb.answer('Сохранено')
    await show(cb)


@router.callback_query(F.data.startswith('dc:reset:'))
async def reset(cb: CallbackQuery):
    await service.mark(cb.data.split(':')[-1],'new');await show(cb)


@router.callback_query(F.data == 'dc:export')
async def export(cb: CallbackQuery):
    async with Session() as s:data=await service.load(s)
    await cb.message.answer_document(BufferedInputFile(service.export(data),filename='dagestan-business-contacts.csv'))
    await cb.answer()


@router.callback_query(F.data == 'dc:home')
async def home(cb: CallbackQuery):
    from app.bot.setup import admin_panel
    await reply(cb, '🛠 <b>Управление ботом</b>', admin_panel(owner=True))
