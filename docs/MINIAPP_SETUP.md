# Включение мини-приложения — по шагам (с телефона, Termius)

Особенность вашего сервера: порты 80/443 и HTTPS уже держит Caddy бота модерации (`tg-group-bot-caddy-1`), и на адресе
`213-148-11-155.sslip.io` у него своя веб-панель. Поэтому CRM-кабинет живёт на **поддомене** `crm.213-148-11-155.sslip.io`
(sslip.io отдаёт любой поддомен на тот же IP) и подключается к тому же Caddy: второй сервер на 80/443 не ставим.
Бот модерации и его код не меняются; в его `docker/Caddyfile` добавляется один блок про CRM.

## Шаг A. CRM (наша часть)
```
cd ~/CRM-bot-tg
printf '\nWEBAPP_URL=https://crm.213-148-11-155.sslip.io\nCOMPOSE_FILE=docker-compose.yml:docker-compose.web.yml\n' >> .env
git pull && docker compose up -d --build
sleep 20 && docker compose logs --tail 15
```
В логе: `мини-приложение: порт 8091`. Если бот не стартует и пишет про сеть `tg-group-bot_default` — выполните `docker network ls` и пришлите вывод.

## Шаг B. Один блок в Caddy бота модерации (с вашего разрешения)
```
cd ~/tg-group-bot
cp docker/Caddyfile docker/Caddyfile.bak
printf '\ncrm.213-148-11-155.sslip.io {\n\treverse_proxy crm-web:8091\n}\n' >> docker/Caddyfile
docker exec tg-group-bot-caddy-1 caddy validate --config /etc/caddy/Caddyfile
```
Если проверка пишет `Valid configuration` — применить без перезапуска и без остановки веб-панели бота модерации:
```
docker exec tg-group-bot-caddy-1 caddy reload --config /etc/caddy/Caddyfile
```
Если проверка пишет ошибку — ничего не применять, вернуть как было: `cp docker/Caddyfile.bak docker/Caddyfile`.

## Шаг C. Проверить
- В браузере телефона: `https://crm.213-148-11-155.sslip.io/healthz` → `{"ok": true}` (сертификат выдаётся до минуты).
- В боте: `/app` или 👤 Профиль → **📱 Кабинет**.
- Веб-панель бота модерации (`https://213-148-11-155.sslip.io/...`) открывается как раньше.

## Откат
Удалить из `.env` две строки (`WEBAPP_URL`, `COMPOSE_FILE`) и `docker compose up -d`. Блок в Caddyfile можно оставить или вернуть из `.bak` и сделать `caddy reload`.

## Важно знать
- Кабинет подключается к сети `tg-group-bot_default`, которую создаёт стек бота модерации. Если когда-нибудь выполнить `docker compose down` в `~/tg-group-bot`
  (сеть удалится), то `docker compose up` в CRM остановится с ошибкой про сеть. Чинится так: сначала поднять бота модерации (`docker compose up -d` в `~/tg-group-bot`),
  либо временно убрать из `.env` строку `COMPOSE_FILE=…` — CRM запустится без кабинета.
- Обычный `restart`, обновление (`git pull && docker compose up -d --build`) и перезагрузка сервера на это не влияют.
- Если CRM остановлен, кабинет отвечает ошибкой 502 только на своём адресе, веб-панель бота модерации работает как раньше.
