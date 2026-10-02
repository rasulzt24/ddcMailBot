"""IMAP IDLE: сервер сам сообщает о новом письме или смене флагов — синхронизация запускается сразу.

На каждый ящик — отдельный поток с постоянным соединением к «Входящим». Сам поток письма не загружает
и в БД не ходит: только «пинает» воркер, а забирает письма обычная синхронизация.
Опрос по расписанию остаётся страховкой (реже, пока IDLE работает).
"""
import imaplib
import logging
import threading
from typing import Callable

from .imap import ImapAuthError, ImapSession

logger = logging.getLogger(__name__)

SUPPORTED = hasattr(imaplib.IMAP4, "idle")  # встроенный IDLE — Python 3.14+
IDLE_CHUNK = 5 * 60  # переотправляем IDLE (RFC 2177 — не реже 29 мин), заодно держим соединение «живым» через VPN/NAT
CHANGE_RESPONSES = {"EXISTS", "EXPUNGE", "FETCH", "RECENT"}
MAX_RECONNECT_DELAY = 300


class IdleWatcher(threading.Thread):
    def __init__(self, account_id: int, host: str, port: int, login: str, password: str,
                 on_change: Callable[[int], None]):
        super().__init__(daemon=True, name=f"idle-{account_id}")
        self.account_id = account_id
        self.host, self.port, self.login, self.password = host, port, login, password
        self.on_change = on_change
        self.stop_event = threading.Event()
        self.healthy = False

    def stop(self):
        self.stop_event.set()

    def run(self):
        delay = 5
        while not self.stop_event.is_set():
            try:
                with ImapSession(self.host, self.port, self.login, self.password) as imap:
                    if "IDLE" not in imap.conn.capabilities:
                        logger.warning("account %s: server doesn't support IDLE, polling only", self.account_id)
                        return
                    imap.select("INBOX")
                    self.healthy, delay = True, 5
                    # После (пере)подключения — сразу сверка: пока связи не было, события могли пропасть
                    self.on_change(self.account_id)
                    while not self.stop_event.is_set():
                        if self._wait_change(imap.conn):
                            self.on_change(self.account_id)
            except ImapAuthError as e:
                # Пароль не подходит — не повторяем, чтобы AD не заблокировал учётку.
                # Воркер перезапустит поток, когда пароль в ящике сменится.
                logger.warning("account %s: IDLE auth failed, stopped: %s", self.account_id, e)
                self.healthy = False
                return
            except Exception as e:
                if not self.stop_event.is_set():
                    logger.info("account %s: IDLE connection lost (%s), reconnect in %ss", self.account_id, e, delay)
            self.healthy = False
            self.stop_event.wait(delay)
            delay = min(delay * 2, MAX_RECONNECT_DELAY)

    def _wait_change(self, conn: imaplib.IMAP4) -> bool:
        with conn.idle(duration=IDLE_CHUNK) as idler:
            for typ, _ in idler:
                if typ in CHANGE_RESPONSES:
                    return True
                if self.stop_event.is_set():
                    break
        return False
