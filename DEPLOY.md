# Развёртывание на Linux-сервере (Docker)

Бот и воркер работают в Docker на том же сервере, где Postgres, Redis и SeaweedFS.
VPN (strongSwan) настраивается на хосте отдельно — контейнеры используют сеть хоста (`network_mode: host`)
и ходят к почте через тот же туннель.

```
сервер
├── strongSwan (хост) ──── IPsec ───► mail.nationalbank.kz (192.168.80.233), IMAP 993 / SMTP 587
├── Postgres :5433, Redis :6379, SeaweedFS S3 :9000   ← по 127.0.0.1
└── docker compose
    ├── migrate  — миграции БД, запускается и завершается
    ├── worker   — python manage.py mail_worker   (ровно один!)
    └── bot      — python manage.py runbot
```

## 1. Что нужно на сервере

- Docker Engine + плагин `docker compose`.
- Поднятый VPN. Проверка с хоста (без этого дальше нет смысла):
  ```bash
  nc -zv 192.168.80.233 993    # IMAP
  nc -zv 192.168.80.233 587    # SMTP
  ```

## 2. Перенос файлов проекта

С Windows-машины (в PowerShell, из папки `mailDDC`) — архив без venv, логов и пустой `media`:

```powershell
tar --exclude=mailbot/.venv --exclude=mailbot/media --exclude="mailbot/logs_*.txt" --exclude=__pycache__ -czf mailbot.tgz mailbot
scp mailbot.tgz user@85.214.182.135:/opt/
```

На сервере:

```bash
cd /opt && tar -xzf mailbot.tgz && cd mailbot
ls .env certs/mail_trusted.pem     # оба должны быть на месте
# В контейнере процесс работает от пользователя с UID 1000 — .env должен быть читаем для него и закрыт для остальных
sudo chown 1000:1000 .env && chmod 600 .env
```

## 3. Правки `.env` для сервера

Всё работает на одном сервере — адреса меняются на локальные:

```env
DJANGO_DEBUG=False
DB_HOST=127.0.0.1
DB_PORT=5433
REDIS_URL=redis://:ПАРОЛЬ@127.0.0.1:6379/0
S3_ENDPOINT_URL=http://127.0.0.1:9000
```

Остальное (токен бота, `MAIL_ENCRYPTION_KEY`, ключи S3) — без изменений.
**`MAIL_ENCRYPTION_KEY` менять нельзя**: иначе сохранённые пароли ящиков не расшифруются.

## 4. Остановить бота на Windows

Одновременно может работать только **один** бот и **один** воркер с этим токеном и базой:
два бота конфликтуют в Telegram (`TelegramConflictError`), два воркера дублируют синхронизацию.
Остановите `runbot` и `mail_worker` на Windows-машине перед запуском на сервере.

## 5. Запуск

```bash
docker compose up -d --build
docker compose ps                    # migrate — exited (0), worker и bot — running
docker compose logs -f worker bot
```

В логе воркера должно быть:

```
Mail worker started: poll=30s threads=8 idle=True
Subscribed to Redis channel mailbot:worker
```

`idle=True` — работают мгновенные уведомления (IMAP IDLE). В логе бота — `Run polling for bot @DDCMailBot`.

## 6. Обновление

```bash
cd /opt/mailbot
# заменить файлы проекта (новый архив или git pull), .env и certs/ не трогать
docker compose up -d --build         # пересоберёт образ, применит миграции, перезапустит
docker image prune -f                # удалить старые образы
```

## 7. Повседневные команды

| Что | Команда |
|---|---|
| Логи | `docker compose logs -f --tail=100 worker` |
| Перезапуск | `docker compose restart worker bot` |
| Остановить | `docker compose down` |
| Django shell | `docker compose run --rm worker python manage.py shell` |
| Суперпользователь для /admin/ | `docker compose run --rm worker python manage.py createsuperuser` |
| Перенос файлов в S3 | `docker compose run --rm worker python manage.py mail_move_storage` |

Контейнеры поднимаются сами после перезагрузки сервера (`restart: unless-stopped`) — нужно лишь,
чтобы Docker был включён в автозапуск: `systemctl enable docker`.

## 8. После переезда — закрыть порты наружу

Когда бот работает на сервере, Postgres (5433), Redis (6379) и SeaweedFS (9000) снаружи не нужны.
Сейчас они открыты в интернет, а S3 — ещё и без шифрования. Варианты:

- в их `docker-compose`/конфиге публиковать порты только на localhost: `"127.0.0.1:5433:5432"`;
- или закрыть файрволом: `ufw deny 5433`, `ufw deny 6379`, `ufw deny 9000`.

Внимание: Docker публикует порты в обход `ufw`, поэтому для контейнеров надёжнее первый вариант.
Для доступа с рабочей машины (например, к БД из IDE) — SSH-туннель:
`ssh -L 5433:127.0.0.1:5433 user@85.214.182.135`.

## 9. Бэкапы

Все данные теперь на этом сервере — бэкапить вместе и регулярно:

- Postgres: `pg_dump` (письма, ящики, зашифрованные пароли);
- том SeaweedFS (вложения);
- `.env` — без `MAIL_ENCRYPTION_KEY` пароли из бэкапа БД не восстановить.

## Если что-то не работает

| Симптом в логах | Причина |
|---|---|
| `sync failed: [Errno 110] Connection timed out` / `WinError 10060` | VPN не поднят или туннель не пропускает трафик |
| `sync failed` на `194.88.202.26` | DNS отдал внешний адрес — проверьте `extra_hosts` в `docker-compose.yml` |
| `CERTIFICATE_VERIFY_FAILED` | нет `certs/mail_trusted.pem` или не задан `MAIL_SSL_CAFILE=certs/mail_trusted.pem` |
| `TelegramConflictError` | бот ещё запущен где-то ещё (например, на Windows) |
| `idle=False` | образ собран не на Python 3.14 — уведомления будут работать через опрос раз в 30 с |
| `DB unavailable` | Postgres не слушает `127.0.0.1:5433` — проверьте `DB_HOST`/`DB_PORT` |

## Справочник сотрудников (подсказки адресатов)

Бот ищет людей в адресной книге Exchange (EWS, `https://mail.nationalbank.kz/EWS/Exchange.asmx`)
под учётной записью пользователя. Что нужно:

- **Порт 443 до почтового сервера через туннель** (кроме 993 и 587):
  `nc -zv 192.168.80.233 443`
- **Inline-режим у бота:** @BotFather → `/setinline` → выбрать бота → подсказка, например `Имя или фамилия…`.
  Без него кнопка «🔎 Найти в справочнике» не покажет подсказки (поиск по имени текстом всё равно работает).
- Формат логина подбирается сам из `MAIL_DIRECTORY_DOMAINS` (по умолчанию `BSB,NB`) и запоминается
  для ящика (`directory_login` в админке). Если не подошёл ни один — повтор через сутки, чтобы не
  заблокировать учётку. Отключить справочник: `MAIL_DIRECTORY_ENABLED=False`.

## Рабочий день (Битрикс, учёт времени)

Кнопка «⏱ Рабочий день»: начать/завершить день в Битриксе сейчас или с указанным временем,
напоминания по будням. Бот входит на портал под логином и паролем от почты (домен BSB);
если они не подходят — спросит отдельные. После отказа входа сам больше не пробует.

- Портал: `https://portal.bsbnb.kz` (= `portal.bsb.nb.rk`, `10.10.32.53`) — сертификат `*.bsbnb.kz`
  выдан публичным CA, поэтому используется имя `portal.bsbnb.kz`.
- **Нужен доступ с сервера через VPN к `10.10.32.53:443`:** `nc -zv 10.10.32.53 443`.
- Если DNS сервера не знает имя — добавить в `extra_hosts` в `docker-compose.yml`:
  `- "portal.bsbnb.kz:10.10.32.53"`.
- Настройки (необязательные): `BITRIX_ENABLED`, `BITRIX_URL`, `BITRIX_SITE_ID=s1`.
