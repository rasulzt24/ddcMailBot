import base64
import imaplib
import re
import ssl
import time

from django.conf import settings

IMAP_TIMEOUT = 60
SENT_FOLDER_CANDIDATES = ("sent items", "sent", "отправленные", "sent messages", "inbox.sent")

_UID_RE = re.compile(rb"UID (\d+)")
_LIST_RE = re.compile(rb'\((?P<flags>[^)]*)\)\s+(?P<delim>"[^"]*"|NIL)\s+(?P<name>.+)$')


class ImapAuthError(Exception):
    """Неверный логин/пароль — повторять нельзя, чтобы не заблокировать учётку AD."""


def ssl_context(check_hostname: bool = True) -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if settings.MAIL_SSL_CAFILE:
        ctx.load_verify_locations(cafile=settings.MAIL_SSL_CAFILE)
        # Разрешаем доверять самому сертификату сервера (pinning), без полной цепочки до корня
        ctx.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
        ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
    if not check_hostname:
        ctx.check_hostname = False
    if not settings.MAIL_SSL_VERIFY:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def decode_imap_utf7(name: str) -> str:
    """Modified UTF-7 (RFC 3501) — так IMAP кодирует кириллические имена папок."""
    def repl(m):
        chunk = m.group(1)
        if not chunk:
            return "&"
        b64 = chunk.replace(",", "/")
        b64 += "=" * (-len(b64) % 4)
        return base64.b64decode(b64).decode("utf-16-be")
    return re.sub(r"&([^-]*)-", repl, name)


def _quote(folder: str) -> str:
    if folder.startswith('"'):
        return folder
    return '"' + folder.replace("\\", "\\\\").replace('"', '\\"') + '"'


class ImapSession:
    def __init__(self, host: str, port: int, login: str, password: str):
        self.host, self.port, self.login_name, self.password = host, port, login, password
        self.conn: imaplib.IMAP4_SSL | None = None
        self.selected: tuple[str, bool] | None = None

    @classmethod
    def for_account(cls, account) -> "ImapSession":
        return cls(account.imap_host, account.imap_port, account.login, account.password)

    def __enter__(self):
        self.conn = imaplib.IMAP4_SSL(self.host, self.port, ssl_context=ssl_context(), timeout=IMAP_TIMEOUT)
        try:
            self.conn.login(self.login_name, self.password)
        except imaplib.IMAP4.error as e:
            self._safe_logout()
            raise ImapAuthError(str(e)) from e
        return self

    def __exit__(self, *exc):
        self._safe_logout()

    def _safe_logout(self):
        if self.conn is not None:
            try:
                self.conn.logout()
            except Exception:
                pass
            self.conn = None

    def select(self, folder: str = "INBOX", readonly: bool = True) -> int:
        """Выбирает папку, возвращает UIDVALIDITY."""
        typ, data = self.conn.select(_quote(folder), readonly=readonly)
        if typ != "OK":
            raise imaplib.IMAP4.error(f"SELECT {folder}: {data}")
        self.selected = (folder, readonly)
        _, validity = self.conn.response("UIDVALIDITY")
        return int(validity[0]) if validity and validity[0] else 0

    def all_uids(self) -> list[int]:
        typ, data = self.conn.uid("search", None, "ALL")
        return sorted(int(x) for x in (data[0] or b"").split())

    def uids_after(self, last_uid: int) -> list[int]:
        typ, data = self.conn.uid("search", None, f"UID {last_uid + 1}:*")
        # Диапазон N:* всегда включает последнее письмо, даже если его UID < N
        return sorted(u for u in (int(x) for x in (data[0] or b"").split()) if u > last_uid)

    def fetch(self, uid: int) -> tuple[bytes | None, set[str]]:
        """Письмо целиком без установки флага \\Seen."""
        typ, data = self.conn.uid("fetch", str(uid), "(FLAGS BODY.PEEK[])")
        if typ != "OK":
            return None, set()
        raw, flags = None, set()
        for item in data:
            if isinstance(item, tuple):
                raw = item[1]
                flags |= {f.decode() for f in imaplib.ParseFlags(item[0])}
            elif isinstance(item, bytes) and b"FLAGS" in item:
                flags |= {f.decode() for f in imaplib.ParseFlags(item)}
        return raw, flags

    def flags_since(self, min_uid: int) -> dict[int, set[str]]:
        """Флаги всех писем выбранной папки с UID >= min_uid — один лёгкий запрос, без тел писем."""
        typ, data = self.conn.uid("fetch", f"{min_uid}:*", "(UID FLAGS)")
        if typ != "OK":
            return {}
        result = {}
        for item in data or []:
            line = item[0] if isinstance(item, tuple) else item
            if not isinstance(line, bytes):
                continue
            m = _UID_RE.search(line)
            if m:
                result[int(m.group(1))] = {f.decode() for f in imaplib.ParseFlags(line)}
        return result

    def set_seen(self, uid: int, seen: bool, folder: str = "INBOX"):
        if self.selected != (folder, False):
            self.select(folder, readonly=False)
        self.conn.uid("store", str(uid), "+FLAGS" if seen else "-FLAGS", r"(\Seen)")

    def list_folders(self) -> list[tuple[set[str], str]]:
        typ, data = self.conn.list()
        result = []
        for line in data or []:
            if not isinstance(line, bytes):
                continue
            m = _LIST_RE.match(line)
            if not m:
                continue
            flags = {f.lower() for f in m.group("flags").decode(errors="ignore").split()}
            name = m.group("name").decode(errors="ignore").strip()
            if name.startswith('"') and name.endswith('"'):
                name = name[1:-1].replace('\\"', '"').replace("\\\\", "\\")
            result.append((flags, name))
        return result

    def find_sent_folder(self) -> str | None:
        folders = self.list_folders()
        for flags, name in folders:
            if "\\sent" in flags:
                return name
        by_name = {decode_imap_utf7(name).lower(): name for _, name in folders}
        for candidate in SENT_FOLDER_CANDIDATES:
            if candidate in by_name:
                return by_name[candidate]
        return None

    def append(self, folder: str, raw: bytes):
        typ, data = self.conn.append(_quote(folder), r"(\Seen)", imaplib.Time2Internaldate(time.time()), raw)
        if typ != "OK":
            raise imaplib.IMAP4.error(f"APPEND {folder}: {data}")


def check_login(host: str, port: int, login: str, password: str) -> None:
    with ImapSession(host, port, login, password) as s:
        s.select("INBOX")
