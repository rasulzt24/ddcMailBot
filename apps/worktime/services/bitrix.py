"""Учёт рабочего времени в коробочном Битрикс24 (модуль timeman) — так же, как это делает виджет портала.

POST /bitrix/tools/timeman.php?action=<update|open|close>&site_id=s1&sessid=<sessid>
  timestamp — время в секундах от полуночи (часовой пояс пользователя), recordId — запись дня.
Ответ — JS-объект с одинарными кавычками: {'ID':'77657','STATE':'OPENED',...}.

Вызывать из отдельного потока: это сетевые запросы.
"""
import ast
import logging
import re
import threading
import time as monotime
from dataclasses import dataclass, field
from datetime import datetime, time

import requests
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

TIMEOUT = 20
SESSION_TTL = 20 * 60
_JS_LITERAL = re.compile(r"(?<=[:\[,])\s*(true|false|null)(?=\s*[,}\]])")
_SESSID = re.compile(r"['\"]bitrix_sessid['\"]\s*:\s*['\"]([0-9a-f]+)['\"]")
_USER_ID = re.compile(r"['\"]USER_ID['\"]\s*:\s*['\"]?(\d+)")
_ERROR_TEXT = re.compile(r'class="errortext"[^>]*>([^<]+)')


class BitrixError(Exception):
    pass


class BitrixAuthError(BitrixError):
    """Неверный логин/пароль — автоматически не повторяем (блокировка учётки в AD)."""


@dataclass
class DayState:
    state: str  # OPENED | CLOSED | PAUSED | EXPIRED
    record_id: str
    start: datetime | None
    finish: datetime | None
    duration: int  # секунд
    raw: dict = field(repr=False, default_factory=dict)

    @property
    def is_open(self) -> bool:
        return self.state in ("OPENED", "PAUSED", "EXPIRED")

    @property
    def started_today(self) -> bool:
        return bool(self.start and self.start.date() == timezone.localdate())

    @property
    def unclosed_previous_day(self) -> bool:
        """День начат не сегодня и не закрыт — Битрикс требует закрыть его с указанием времени."""
        return self.is_open and bool(self.start) and self.start.date() < timezone.localdate()

    @property
    def finished_today(self) -> bool:
        return self.state == "CLOSED" and self.started_today


def _parse(text: str) -> dict:
    body = text.strip()
    if not body.startswith("{"):
        raise BitrixError(f"Неожиданный ответ Битрикса: {body[:120]}")
    body = _JS_LITERAL.sub(lambda m: {"true": "True", "false": "False", "null": "None"}[m.group(1)], body)
    body = body.replace("\\/", "/")
    try:
        data = ast.literal_eval(body)
    except (ValueError, SyntaxError) as e:
        raise BitrixError(f"Не удалось разобрать ответ Битрикса: {e}") from e
    if not isinstance(data, dict):
        raise BitrixError("Неожиданный ответ Битрикса")
    if data.get("error"):
        raise BitrixError(str(data["error"]))
    return data


def _local(ts) -> datetime | None:
    try:
        ts = int(ts)
    except (TypeError, ValueError):
        return None
    return timezone.localtime(datetime.fromtimestamp(ts, tz=timezone.get_current_timezone())) if ts else None


def _state(data: dict) -> DayState:
    info = data.get("INFO") or {}
    try:
        duration = int(info.get("DURATION") or 0)
    except ValueError:
        duration = 0
    return DayState(state=str(data.get("STATE") or info.get("CURRENT_STATUS") or ""),
                    record_id=str(data.get("ID") or ""), start=_local(info.get("DATE_START")),
                    finish=_local(info.get("DATE_FINISH")), duration=duration, raw=data)


def seconds_of_day(t: time) -> int:
    return t.hour * 3600 + t.minute * 60


_sessions: dict[str, tuple[float, requests.Session, str, int | None]] = {}
_lock = threading.Lock()


def forget(login: str) -> None:
    with _lock:
        hit = _sessions.pop(login.lower(), None)
    if hit:
        hit[1].close()


class BitrixClient:
    def __init__(self, login: str, password: str):
        self.login = login
        self.password = password
        self.base = settings.BITRIX_URL.rstrip("/")

    def _new_session(self) -> tuple[requests.Session, str, int | None]:
        s = requests.Session()
        s.headers["User-Agent"] = "Mozilla/5.0 (MailDDC bot)"
        s.verify = settings.BITRIX_SSL_VERIFY
        r = s.post(f"{self.base}/?login=yes", timeout=TIMEOUT, data={
            "AUTH_FORM": "Y", "TYPE": "AUTH", "backurl": "/", "USER_LOGIN": self.login,
            "USER_PASSWORD": self.password, "USER_REMEMBER": "N",
        })
        r.raise_for_status()
        sessid, user_id = _SESSID.search(r.text), _USER_ID.search(r.text)
        if 'name="form_auth"' in r.text or not sessid or not user_id or user_id.group(1) == "0":
            err = _ERROR_TEXT.search(r.text)
            raise BitrixAuthError(err.group(1).strip() if err else "Неверный логин или пароль Битрикса")
        return s, sessid.group(1), int(user_id.group(1))

    def _session(self, fresh: bool = False) -> tuple[requests.Session, str, int | None]:
        key = self.login.lower()
        with _lock:
            hit = _sessions.get(key)
            if hit and not fresh and monotime.monotonic() - hit[0] < SESSION_TTL:
                return hit[1], hit[2], hit[3]
        s, sessid, user_id = self._new_session()
        with _lock:
            _sessions[key] = (monotime.monotonic(), s, sessid, user_id)
        return s, sessid, user_id

    @property
    def user_id(self) -> int | None:
        return self._session()[2]

    def _call(self, action: str, data: dict) -> DayState:
        for attempt in range(2):
            s, sessid, _ = self._session(fresh=attempt > 0)
            r = s.post(f"{self.base}/bitrix/tools/timeman.php", timeout=TIMEOUT,
                       params={"action": action, "site_id": settings.BITRIX_SITE_ID, "sessid": sessid},
                       data=data, headers={"BX-AJAX": "true"})
            r.raise_for_status()
            # Сессия истекла — Битрикс отдаёт форму входа или пустой ответ: входим заново один раз
            if attempt == 0 and (not r.text.strip() or 'name="form_auth"' in r.text):
                continue
            return _state(_parse(r.text))
        raise BitrixError("Битрикс не принял сессию")

    def status(self) -> DayState:
        return self._call("update", {"device": "browser"})

    def open(self, at: time, record_id: str = "") -> DayState:
        return self._call("open", {"timestamp": seconds_of_day(at), "report": "", "recordId": record_id,
                                   "device": "browser"})

    def close(self, at: time, record_id: str, report: str = "") -> DayState:
        return self._call("close", {"timestamp": seconds_of_day(at), "report": report, "recordId": record_id,
                                    "device": "browser"})
