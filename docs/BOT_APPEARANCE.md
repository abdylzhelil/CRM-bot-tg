# Внешность бота — что уже в коде и что сделать в BotFather

## Само ставится при старте бота (`setup.setup_profile`)
Имя «КлиентРадар», «Что умеет этот бот» (Description), короткое «О боте» (About), список команд (у админов длиннее), кнопка «Меню».
Тексты — `setup.BOT_NAME / DESCRIPTION / SHORT_DESCRIPTION`; экран `/about` — `setup.ABOUT`; приветствие нового — `profile.WELCOME`.

## Только руками в @BotFather (API бота это не умеет)
Готовые картинки лежат в `docs/brand/` (радар, 640×640 и 640×360). Скачайте их на телефон из GitHub (файл → Download).
1. `/mybots` → @klientradarbot → **Edit Bot** → **Edit Botpic** → отправить `avatar.png`.
2. **Edit Bot** → **Edit Description Picture** → отправить `description_picture.png` (показывается над «Запустить» в пустом чате).
3. Остальное (имя, описания, команды) бот выставит сам после обновления — ничего вводить не надо.

## Свои картинки (промпт для нейросети)
Аватар: «Minimal flat icon, radar screen on deep navy background, cyan concentric circles, sweeping translucent cyan sector,
three yellow dots as targets, white centre dot, no text, square 1:1».
Картинка-описание: тот же стиль, формат 16:9, слева радар, справа место под текст.

## Обновить на сервере
`cd ~/CRM-bot-tg && git pull && docker compose up -d --build`
