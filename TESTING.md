# Тестовая среда

Полностью отдельная от рабочей: своя БД, Redis, S3 (SeaweedFS) и почтовый сервер GreenMail
с тестовыми ящиками. Рабочие данные, почта банка и рабочий бот не затрагиваются.

| Что | Тестовая среда | Рабочая |
|---|---|---|
| Настройки | `.env.test` | `.env` |
| БД | Postgres `127.0.0.1:55432`, база `mailbot_test` | `85.214.182.135:5433` |
| Redis | `127.0.0.1:56379` | `85.214.182.135:6379` |
| Файлы | SeaweedFS `127.0.0.1:18333`, бакет `mailbot-test` | MinIO/SeaweedFS сервера |
| Почта | GreenMail: IMAPS `3993`, SMTPS `3465` | `mail.nationalbank.kz` |
| Бот | **отдельный тестовый бот** | @DDCMailBot |

Тестовые ящики (пароль у всех `test123`):
`me@test.local` — ваш ящик для бота, `colleague@test.local`, `boss@test.local`.

## Один раз

1. Создайте тестового бота у @BotFather (`/newbot`) и вставьте токен в `.env.test` → `TELEGRAM_BOT_TOKEN`.
   Рабочий токен использовать нельзя: два бота с одним токеном конфликтуют.
2. Поднимите контейнеры и подготовьте БД:

```powershell
cd C:\Users\rasul\OneDrive\Desktop\python\mailDDC\mailbot
docker compose -f docker-compose.test.yml up -d
$env:ENV_FILE=".env.test"
..\.venv\Scripts\python manage.py migrate
..\.venv\Scripts\python manage.py mail_seed_test --extra 15
```

## Запуск бота на тестовой среде

Два окна PowerShell, в каждом сначала `$env:ENV_FILE=".env.test"`:

```powershell
cd C:\Users\rasul\OneDrive\Desktop\python\mailDDC\mailbot
$env:ENV_FILE=".env.test"
..\.venv\Scripts\python manage.py mail_worker
```
```powershell
cd C:\Users\rasul\OneDrive\Desktop\python\mailDDC\mailbot
$env:ENV_FILE=".env.test"
..\.venv\Scripts\python manage.py runbot
```

В тестовом боте: `/start` → «📬 Моя почта» → «Подключить» → `me@test.local` / `test123`.
Письма, уже лежащие в ящике, загрузятся как история (без уведомлений).

**Проверить уведомления:** при запущенных боте и воркере ещё раз выполните
`..\.venv\Scripts\python manage.py mail_seed_test` — придут новые карточки.

`ENV_FILE` действует только в текущем окне PowerShell. В новом окне без него — рабочая среда.

## Что лежит в тестовых письмах

Новое письмо с высокой важностью (вы в копии) · вложения PDF/ZIP/PNG/TXT · HTML с картинкой в подписи ·
цепочка «письмо → ваш ответ → ответ на ответ» · пересылка с вложенным `.eml` · автоответ ·
приглашение на встречу (`.ics`) · длинное письмо · свежее письмо · `--extra N` простых писем для страниц списка.

Ответы и новые письма из бота уходят в GreenMail и наружу не попадают.
Неверный пароль можно проверить, подключив ящик с паролем не `test123`.

## Полезное

| Что | Команда |
|---|---|
| Статус контейнеров | `docker compose -f docker-compose.test.yml ps` |
| Логи почтового сервера | `docker compose -f docker-compose.test.yml logs -f mail` |
| Остановить (данные сохраняются) | `docker compose -f docker-compose.test.yml stop` |
| Сбросить всё к чистому состоянию | `docker compose -f docker-compose.test.yml down -v`, затем шаг 2 заново |
| Админка тестовой БД | `$env:ENV_FILE=".env.test"; ..\.venv\Scripts\python manage.py createsuperuser`, затем `runserver` → http://127.0.0.1:8000/admin/ |

GreenMail хранит письма в памяти: после перезапуска контейнера `mail` ящики пустые
(БД при этом остаётся) — выполните `mail_seed_test` снова.
