# Образ для бота и почтового воркера (один образ, разные команды — см. docker-compose.yml).
# Python 3.14+ нужен для IMAP IDLE (мгновенные уведомления о новых письмах).
FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Almaty

# tzdata — локальное время в логах и для zoneinfo
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

RUN useradd --system --uid 1000 --no-create-home mailbot
USER mailbot

CMD ["python", "manage.py", "runbot"]
