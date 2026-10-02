"""Фоновый воркер: синхронизация IMAP-ящиков и отправка исходящих писем.

Запуск: python manage.py mail_worker
"""
import logging
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import OperationalError, close_old_connections, connections, transaction
from django.db.models import Q
from django.utils import timezone

from apps.accounts.models import TelegramUser
from apps.mail.models import MailAccount, OutgoingMessage
from apps.mail.services import events, idle
from apps.mail.services.compose import send_outgoing
from apps.mail.services.sync import run_sync

logger = logging.getLogger("mail_worker")

IDLE_RECONCILE_INTERVAL = 30  # как часто сверять потоки IDLE со списком активных ящиков


def _in_thread(fn, *args):
    # Потоки пула переиспользуются — держим их соединения с БД (CONN_MAX_AGE),
    # переподключение к удалённой БД дорогое. Протухшие закрываются здесь.
    close_old_connections()
    try:
        fn(*args)
    except OperationalError as e:
        logger.warning("%s%s: DB unavailable: %s", fn.__name__, args, e)
        connections.close_all()
    except Exception:
        logger.exception("%s%s failed", fn.__name__, args)


def _active_accounts():
    return MailAccount.objects.filter(is_active=True).filter(
        Q(owner__status=TelegramUser.Status.ACTIVE) | Q(owner__is_superadmin=True))


class Worker:
    def __init__(self):
        self.stop = threading.Event()
        self.wakeup = threading.Event()
        self.pool = ThreadPoolExecutor(max_workers=settings.MAIL_WORKER_THREADS, thread_name_prefix="mail")
        self.syncing: set[int] = set()
        self.sending: set[int] = set()
        self.forced: set[int] = set()  # ящики, о которых сообщил IDLE, — синхронизировать вне расписания
        self.lock = threading.Lock()
        self.watchers: dict[int, tuple[tuple, idle.IdleWatcher]] = {}
        self.idle_reconciled_at = 0.0
        self.use_idle = settings.MAIL_IDLE and idle.SUPPORTED

    # --- события от бота через Redis ---
    def listen_events(self):
        if not settings.REDIS_URL:
            return
        import redis
        while not self.stop.is_set():
            try:
                client = redis.from_url(settings.REDIS_URL, decode_responses=True, health_check_interval=30,
                                        socket_keepalive=True)
                pubsub = client.pubsub(ignore_subscribe_messages=True)
                pubsub.subscribe(events.WORKER_CHANNEL)
                logger.info("Subscribed to Redis channel %s", events.WORKER_CHANNEL)
                while not self.stop.is_set():
                    if pubsub.get_message(timeout=1.0):
                        self.wakeup.set()
            except Exception as e:
                logger.warning("Redis listener error: %s", e)
                self.stop.wait(5)

    # --- IMAP IDLE ---
    def kick(self, account_id: int) -> None:
        """Вызывается из потока IDLE: на сервере что-то изменилось — синхронизировать сейчас."""
        with self.lock:
            self.forced.add(account_id)
        self.wakeup.set()

    def _idle_healthy(self, account_id: int) -> bool:
        entry = self.watchers.get(account_id)
        return bool(entry and entry[1].is_alive() and entry[1].healthy)

    def reconcile_idle(self) -> None:
        """Поток IDLE на каждый активный ящик; перезапуск при смене пароля/сервера, остановка у отключённых."""
        if not self.use_idle or time.monotonic() - self.idle_reconciled_at < IDLE_RECONCILE_INTERVAL:
            return
        self.idle_reconciled_at = time.monotonic()
        wanted = {acc.pk: acc for acc in _active_accounts().filter(needs_reauth=False)
                  .only("id", "imap_host", "imap_port", "login", "password_encrypted")}
        for pk in list(self.watchers):
            if pk not in wanted:
                self.watchers.pop(pk)[1].stop()
        for pk, acc in wanted.items():
            key = (acc.imap_host, acc.imap_port, acc.login, acc.password_encrypted)
            current = self.watchers.get(pk)
            if current and current[0] == key:
                continue  # тот же ящик и пароль (поток жив или остановлен из-за неверного пароля — не трогаем)
            if current:
                current[1].stop()
            watcher = idle.IdleWatcher(pk, acc.imap_host, acc.imap_port, acc.login, acc.password, self.kick)
            watcher.start()
            self.watchers[pk] = (key, watcher)

    # --- планирование ---
    def _due_accounts(self) -> list[int]:
        now = timezone.now()
        result = []
        for acc in _active_accounts().only("id", "last_sync_at", "error_count"):
            # Пока IDLE на связи, о новых письмах сообщает сервер — плановый опрос лишь страховка
            base = settings.MAIL_IDLE_POLL_INTERVAL if self._idle_healthy(acc.pk) else settings.MAIL_POLL_INTERVAL
            # экспоненциальная пауза при ошибках сети: 30с, 60с, 120с ... до 10 мин
            interval = min(base * (2 ** min(acc.error_count, 5)), max(base, 600))
            if acc.last_sync_at is None or acc.last_sync_at <= now - timedelta(seconds=interval):
                result.append(acc.pk)
        return result

    def _forced_accounts(self) -> list[int]:
        with self.lock:
            forced = set(self.forced)
        if not forced:
            return []
        # Ящик могли отключить или у него сменился пароль — не синхронизируем такие по «пинку»
        allowed = set(_active_accounts().filter(pk__in=forced).values_list("id", flat=True))
        with self.lock:
            self.forced -= forced - allowed
        return sorted(allowed)

    def _claim_outgoing(self) -> list[int]:
        now = timezone.now()
        with transaction.atomic():
            ids = list(
                OutgoingMessage.objects.select_for_update(skip_locked=True)
                .filter(status=OutgoingMessage.Status.QUEUED)
                .filter(Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=now))
                .values_list("id", flat=True)[:20]
            )
            OutgoingMessage.objects.filter(id__in=ids).update(status=OutgoingMessage.Status.SENDING)
        return ids

    def _submit(self, bucket: set[int], item_id: int, fn) -> bool:
        with self.lock:
            if item_id in bucket:
                return False
            bucket.add(item_id)

        def job():
            try:
                _in_thread(fn, item_id)
            finally:
                with self.lock:
                    bucket.discard(item_id)

        self.pool.submit(job)
        return True

    def tick(self):
        close_old_connections()
        for outgoing_id in self._claim_outgoing():
            self._submit(self.sending, outgoing_id, send_outgoing)
        for account_id in self._forced_accounts():
            # Если ящик сейчас синхронизируется — «пинок» остаётся и сработает на следующем тике
            if self._submit(self.syncing, account_id, run_sync):
                with self.lock:
                    self.forced.discard(account_id)
        for account_id in self._due_accounts():
            self._submit(self.syncing, account_id, run_sync)
        self.reconcile_idle()

    def run(self):
        # Письма, «зависшие» в статусе sending после падения прошлого запуска, — обратно в очередь
        OutgoingMessage.objects.filter(status=OutgoingMessage.Status.SENDING).update(
            status=OutgoingMessage.Status.QUEUED)
        threading.Thread(target=self.listen_events, daemon=True, name="redis-listener").start()
        logger.info("Mail worker started: poll=%ss threads=%s idle=%s", settings.MAIL_POLL_INTERVAL,
                    settings.MAIL_WORKER_THREADS, self.use_idle)
        if settings.MAIL_IDLE and not idle.SUPPORTED:
            logger.warning("IMAP IDLE needs Python 3.14+, falling back to polling")
        while not self.stop.is_set():
            try:
                self.tick()
            except OperationalError as e:
                # Сетевой сбой до БД — не шумим трейсбеком, повторим на следующем тике
                logger.warning("DB unavailable: %s", e)
                connections.close_all()
                self.stop.wait(5)
            except Exception:
                logger.exception("Worker tick failed")
                connections.close_all()
            self.wakeup.wait(timeout=3)
            self.wakeup.clear()
        logger.info("Stopping, waiting for running jobs...")
        for _, watcher in self.watchers.values():
            watcher.stop()
        self.pool.shutdown(wait=True)


class Command(BaseCommand):
    help = "Синхронизация почтовых ящиков и отправка писем"

    def handle(self, *args, **options):
        worker = Worker()

        def shutdown(*_):
            worker.stop.set()
            worker.wakeup.set()

        signal.signal(signal.SIGINT, shutdown)
        signal.signal(signal.SIGTERM, shutdown)
        worker.run()
