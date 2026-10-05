"""Справочник сотрудников: адресная книга Exchange (EWS ResolveNames) под учётной записью пользователя.

Exchange не всегда принимает логин в том виде, в каком он работает для IMAP (у ДДЦ учётки в домене BSB,
а Exchange — в домене NB). Поэтому пробуем форматы «ДОМЕН\\логин» из MAIL_DIRECTORY_DOMAINS по очереди,
запоминаем сработавший в MailAccount.directory_login и больше не перебираем. Если не подошёл ни один —
не повторяем сутки (каждая неудачная попытка увеличивает счётчик блокировки учётки в AD).
"""
import logging
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from xml.sax.saxutils import escape

import requests
from django.conf import settings
from requests.adapters import HTTPAdapter
from requests_ntlm import HttpNtlmAuth

from .imap import ssl_context

logger = logging.getLogger(__name__)

NS = {"m": "http://schemas.microsoft.com/exchange/services/2006/messages",
      "t": "http://schemas.microsoft.com/exchange/services/2006/types"}
SOAP = """<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"
 xmlns:t="http://schemas.microsoft.com/exchange/services/2006/types"
 xmlns:m="http://schemas.microsoft.com/exchange/services/2006/messages">
 <soap:Header><t:RequestServerVersion Version="Exchange2016"/></soap:Header>
 <soap:Body><m:ResolveNames ReturnFullContactData="true" SearchScope="ActiveDirectory">
  <m:UnresolvedEntry>{query}</m:UnresolvedEntry></m:ResolveNames></soap:Body></soap:Envelope>"""
TIMEOUT = 15
SESSION_TTL = 600


@dataclass
class Person:
    name: str
    email: str
    title: str = ""
    department: str = ""
    company: str = ""
    phone: str = ""
    source: str = "exchange"  # exchange | history

    @property
    def details(self) -> str:
        return ", ".join(x for x in (self.title, self.department) if x)


class DirectoryAuthError(Exception):
    """Exchange не принял ни один формат логина."""


class _PinnedAdapter(HTTPAdapter):
    """Тот же TLS-контекст, что и для IMAP: закреплённый сертификат mail.nationalbank.kz."""

    def init_poolmanager(self, *args, **kwargs):
        kwargs["ssl_context"] = ssl_context()
        return super().init_poolmanager(*args, **kwargs)


_sessions: dict[tuple[int, str], tuple[float, requests.Session]] = {}
_lock = threading.Lock()


def _session(account_id: int, login: str, password: str) -> requests.Session:
    """Сессия с NTLM держит авторизованное keep-alive соединение — не логинимся на каждую букву."""
    key = (account_id, login)
    with _lock:
        hit = _sessions.get(key)
        if hit and time.monotonic() - hit[0] < SESSION_TTL:
            return hit[1]
        s = requests.Session()
        s.mount("https://", _PinnedAdapter())
        s.verify = settings.MAIL_SSL_VERIFY
        s.auth = HttpNtlmAuth(login, password)
        _sessions[key] = (time.monotonic(), s)
        return s


def forget_sessions(account_id: int) -> None:
    with _lock:
        for key in [k for k in _sessions if k[0] == account_id]:
            _sessions.pop(key)[1].close()


def ews_url(account) -> str:
    return settings.MAIL_DIRECTORY_URL or f"https://{account.imap_host}/EWS/Exchange.asmx"


def login_candidates(account) -> list[str]:
    if account.directory_login:
        return [account.directory_login]
    local = account.login.split("@")[0].split("\\")[-1]
    return [f"{domain}\\{local}" for domain in settings.MAIL_DIRECTORY_DOMAINS]


def _parse(content: bytes) -> list[Person]:
    root = ET.fromstring(content)
    msg = root.find(".//m:ResolveNamesResponseMessage", NS)
    code = msg.findtext("m:ResponseCode", namespaces=NS) if msg is not None else None
    if code == "ErrorNameResolutionNoResults":
        return []
    people = []
    for res in root.findall(".//t:Resolution", NS):
        email = (res.findtext("t:Mailbox/t:EmailAddress", namespaces=NS) or "").strip()
        if not email or "@" not in email:
            continue
        phone = ""
        for entry in res.findall("t:Contact/t:PhoneNumbers/t:Entry", NS):
            if entry.text and entry.get("Key") in ("BusinessPhone", "MobilePhone"):
                phone = entry.text.strip()
                break
        people.append(Person(
            name=(res.findtext("t:Contact/t:DisplayName", namespaces=NS)
                  or res.findtext("t:Mailbox/t:Name", namespaces=NS) or "").strip(),
            email=email.lower(),
            title=(res.findtext("t:Contact/t:JobTitle", namespaces=NS) or "").strip(),
            department=(res.findtext("t:Contact/t:Department", namespaces=NS) or "").strip(),
            company=(res.findtext("t:Contact/t:CompanyName", namespaces=NS) or "").strip(),
            phone=phone,
        ))
    return people


def search_exchange(account, query: str) -> tuple[list[Person], str]:
    """Поиск в адресной книге Exchange. Возвращает (люди, сработавший логин).

    Вызывать из отдельного потока: это сетевой запрос (0.3–1 с).
    """
    body = SOAP.format(query=escape(query.strip()[:100])).encode("utf-8")
    headers = {"Content-Type": "text/xml; charset=utf-8"}
    for login in login_candidates(account):
        session = _session(account.pk, login, account.password)
        try:
            r = session.post(ews_url(account), data=body, headers=headers, timeout=TIMEOUT)
        except requests.RequestException:
            forget_sessions(account.pk)
            raise
        if r.status_code == 401:
            logger.info("%s: Exchange directory rejected login %s", account, login)
            forget_sessions(account.pk)
            continue
        r.raise_for_status()
        return _parse(r.content), login
    raise DirectoryAuthError("Exchange не принял учётные данные для адресной книги")
