import os
from pathlib import Path

from environs import Env

BASE_DIR = Path(__file__).resolve().parent.parent

# ENV_FILE=.env.test — переключиться на тестовую среду (по умолчанию рабочий .env)
ENV_FILE = Path(os.environ.get("ENV_FILE") or BASE_DIR / ".env")
if not ENV_FILE.is_absolute():
    ENV_FILE = BASE_DIR / ENV_FILE
if not ENV_FILE.exists():
    raise RuntimeError(f"Файл настроек не найден: {ENV_FILE}")

env = Env()
env.read_env(str(ENV_FILE))

SECRET_KEY = env.str("DJANGO_SECRET_KEY")
DEBUG = env.bool("DJANGO_DEBUG", False)
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS", ["localhost", "127.0.0.1"])

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "apps.accounts",
    "apps.mail",
    "apps.worktime",
    "bot",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": env.str("DB_NAME", "mailddc"),
        "USER": env.str("DB_USER"),
        "PASSWORD": env.str("DB_PASSWORD"),
        "HOST": env.str("DB_HOST", "127.0.0.1"),
        "PORT": env.int("DB_PORT", 5432),
        # БД удалённая, сеть иногда подтормаживает: держим соединения и даём время на подключение
        "CONN_MAX_AGE": env.int("DB_CONN_MAX_AGE", 300),
        "CONN_HEALTH_CHECKS": True,
        "OPTIONS": {
            "connect_timeout": env.int("DB_CONNECT_TIMEOUT", 30),
            "keepalives": 1,
            "keepalives_idle": 30,
            "keepalives_interval": 10,
            "keepalives_count": 3,
        },
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "ru-ru"
TIME_ZONE = env.str("TIME_ZONE", "Asia/Almaty")
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_URL = "media/"
MEDIA_ROOT = Path(env.str("MEDIA_ROOT", str(BASE_DIR / "media")))

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- Хранилище файлов (вложения, исходящие файлы, .eml) ---
# Пусто -> локальный диск (MEDIA_ROOT). Задан S3_ENDPOINT_URL -> MinIO / любое S3-совместимое хранилище.
S3_ENDPOINT_URL = env.str("S3_ENDPOINT_URL", "")
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
if S3_ENDPOINT_URL:
    STORAGES["default"] = {
        "BACKEND": "storages.backends.s3.S3Storage",
        "OPTIONS": {
            "endpoint_url": S3_ENDPOINT_URL,
            "bucket_name": env.str("S3_BUCKET", "mailbot"),
            "access_key": env.str("S3_ACCESS_KEY"),
            "secret_key": env.str("S3_SECRET_KEY"),
            "region_name": env.str("S3_REGION", "us-east-1"),
            "addressing_style": "path",  # MinIO
            "signature_version": "s3v4",
            "file_overwrite": False,
            "default_acl": None,
            "querystring_auth": True,
            "verify": env.bool("S3_VERIFY_SSL", True),
        },
    }

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "default": {"format": "%(asctime)s %(levelname)s [%(name)s] %(message)s"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "default"},
    },
    "root": {"handlers": ["console"], "level": env.str("LOG_LEVEL", "INFO")},
    "loggers": {
        "aiogram.event": {"level": "WARNING"},
    },
}

# --- Telegram ---
TELEGRAM_BOT_TOKEN = env.str("TELEGRAM_BOT_TOKEN")
SUPERADMIN_TELEGRAM_ID = env.int("SUPERADMIN_TELEGRAM_ID")
# Новые пользователи ждут подтверждения суперадмина
REQUIRE_APPROVAL = env.bool("REQUIRE_APPROVAL", True)

# --- Redis: FSM-хранилище бота + мгновенные события между воркером и ботом.
# Если пусто — бот использует MemoryStorage, а события опрашиваются из БД.
REDIS_URL = env.str("REDIS_URL", "")

# --- Почта ---
MAIL_IMAP_HOST = env.str("MAIL_IMAP_HOST", "mail.nationalbank.kz")
MAIL_IMAP_PORT = env.int("MAIL_IMAP_PORT", 993)
MAIL_SMTP_HOST = env.str("MAIL_SMTP_HOST", "mail.nationalbank.kz")
MAIL_SMTP_PORT = env.int("MAIL_SMTP_PORT", 587)
MAIL_SMTP_SECURITY = env.str("MAIL_SMTP_SECURITY", "starttls")  # starttls | ssl | none
MAIL_SSL_VERIFY = env.bool("MAIL_SSL_VERIFY", True)
# Доп. доверенные сертификаты (PEM): корень NBRK-CA или «закреплённые» сертификаты серверов.
# Создать: python manage.py mail_trust_certs
MAIL_SSL_CAFILE = env.str("MAIL_SSL_CAFILE", "")
if MAIL_SSL_CAFILE and not Path(MAIL_SSL_CAFILE).is_absolute():
    MAIL_SSL_CAFILE = str(BASE_DIR / MAIL_SSL_CAFILE)
# Exchange отдаёт на SMTP самоподписанный сертификат с CN=EX-2 — имя не совпадает с хостом
MAIL_SMTP_CHECK_HOSTNAME = env.bool("MAIL_SMTP_CHECK_HOSTNAME", True)
MAIL_ENCRYPTION_KEY = env.str("MAIL_ENCRYPTION_KEY")  # Fernet-ключ для паролей ящиков
MAIL_POLL_INTERVAL = env.int("MAIL_POLL_INTERVAL", 30)  # сек между проверками ящика
# IMAP IDLE (Python 3.14+): сервер сам сообщает о новых письмах; опрос тогда — лишь страховка раз в N сек
MAIL_IDLE = env.bool("MAIL_IDLE", True)
MAIL_IDLE_POLL_INTERVAL = env.int("MAIL_IDLE_POLL_INTERVAL", 300)
MAIL_INITIAL_IMPORT = env.int("MAIL_INITIAL_IMPORT", 30)  # писем истории при подключении
MAIL_FETCH_BATCH = env.int("MAIL_FETCH_BATCH", 50)
# Сверять «прочитано» с сервером (прочитали в Outlook — отметится и в боте) для писем за N дней. 0 — выключено
MAIL_READ_SYNC_DAYS = env.int("MAIL_READ_SYNC_DAYS", 14)
MAIL_WORKER_THREADS = env.int("MAIL_WORKER_THREADS", 8)
MAIL_SAVE_TO_SENT = env.bool("MAIL_SAVE_TO_SENT", True)
# Хранить оригинал .eml. Он дублирует вложения (~x2 места); без него .eml скачивается с IMAP по запросу
MAIL_STORE_RAW = env.bool("MAIL_STORE_RAW", False)
MAIL_SEND_MAX_ATTEMPTS = env.int("MAIL_SEND_MAX_ATTEMPTS", 3)

# --- Справочник сотрудников: адресная книга Exchange (EWS) под учётной записью пользователя ---
MAIL_DIRECTORY_ENABLED = env.bool("MAIL_DIRECTORY_ENABLED", True)
# Пусто -> https://<MAIL_IMAP_HOST>/EWS/Exchange.asmx
MAIL_DIRECTORY_URL = env.str("MAIL_DIRECTORY_URL", "")
# Домены (NetBIOS) для входа «ДОМЕН\логин», пробуются по порядку, сработавший запоминается для ящика.
# Каждая неудачная попытка увеличивает счётчик блокировки в AD — держите список коротким.
MAIL_DIRECTORY_DOMAINS = env.list("MAIL_DIRECTORY_DOMAINS", ["BSB", "NB"])

# --- Рабочий день: учёт времени в Битриксе (портал BSB, доступен через VPN) ---
BITRIX_ENABLED = env.bool("BITRIX_ENABLED", True)
# portal.bsbnb.kz — тот же сервер, что portal.bsb.nb.rk, но с подходящим сертификатом (*.bsbnb.kz)
BITRIX_URL = env.str("BITRIX_URL", "https://portal.bsbnb.kz")
BITRIX_SITE_ID = env.str("BITRIX_SITE_ID", "s1")
BITRIX_SSL_VERIFY = env.bool("BITRIX_SSL_VERIFY", True)
