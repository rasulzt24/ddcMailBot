"""Разбор RFC822-письма в удобную структуру (заголовки, тело, вложения, тип письма)."""
import email
import hashlib
import mimetypes
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone as dt_timezone
from email.header import decode_header, make_header
from email.message import Message
from email.utils import getaddresses, parsedate_to_datetime

from bs4 import BeautifulSoup

FALLBACK_ENCODINGS = ("utf-8", "windows-1251", "koi8-r", "latin1")

# Префиксы темы: RE:, FW:, Ответ:, Пересл: и т.п. (в т.ч. "RE[2]:")
_REPLY_PREFIX = r"(?:re|aw|ha|отв|ответ)"
_FORWARD_PREFIX = r"(?:fw|fwd|wg|tr|пересл|переслано|пересылка)"
_PREFIX_RE = re.compile(rf"^\s*(?:{_REPLY_PREFIX}|{_FORWARD_PREFIX})\s*(?:\[\d+\])?\s*:\s*", re.I)
REPLY_START = re.compile(rf"^\s*{_REPLY_PREFIX}\s*(?:\[\d+\])?\s*:", re.I)
FORWARD_START = re.compile(rf"^\s*{_FORWARD_PREFIX}\s*(?:\[\d+\])?\s*:", re.I)
_AUTO_SUBJECT = re.compile(r"^\s*(automatic reply|auto(matic)?[- ]?reply|out of office|автоматический ответ|автоответ)", re.I)

# Маркеры начала цитаты / истории переписки в теле
_QUOTE_MARKERS = [
    re.compile(r"^\s*-{2,}\s*(original message|исходное сообщение|пересылаемое сообщение|forwarded message|"
               r"начало переадресованного сообщения)\s*-{2,}\s*$", re.I),
    re.compile(r"^\s*_{8,}\s*$"),
    re.compile(r"^\s*(on|в)\s.+(wrote|пишет|написал\(а\)|написал|написала):\s*$", re.I),
]
_FORWARD_BODY_MARKER = re.compile(r"(пересылаемое сообщение|forwarded message|начало переадресованного сообщения)", re.I)
_FROM_LINE = re.compile(r"^\s*\*?(from|от)\s*:", re.I)
_HEADER_LINE = re.compile(r"^\s*\*?(sent|отправлено|date|дата|to|кому|subject|тема|cc|копия)\s*:", re.I)


@dataclass
class Address:
    name: str
    email: str

    def formatted(self) -> str:
        return format_address(self.name, self.email)


def format_address(name: str, email: str) -> str:
    """'"Имя" <email>' — читаемо в Telegram и корректно разбирается getaddresses (даже с запятой в имени)."""
    name = (name or "").replace('"', "").replace("\\", "").strip()
    return f'"{name}" <{email}>' if name else email


@dataclass
class ParsedAttachment:
    filename: str
    content_type: str
    payload: bytes
    content_id: str = ""
    is_inline: bool = False


@dataclass
class ParsedMessage:
    message_id: str
    in_reply_to: str
    references: list[str]
    subject: str
    thread_topic: str
    from_: Address | None
    reply_to: list[Address]
    to: list[Address]
    cc: list[Address]
    date: datetime
    text: str
    html: str
    new_text: str
    kind: str
    importance: str
    size: int
    attachments: list[ParsedAttachment] = field(default_factory=list)


def _decode_bytes(data: bytes, charset: str | None) -> str:
    for enc in (charset, *FALLBACK_ENCODINGS):
        if not enc:
            continue
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def decode_mime(value) -> str:
    """Декодирование заголовков вида =?utf-8?B?...?=."""
    if not value:
        return ""
    value = str(value)
    try:
        result = str(make_header(decode_header(value)))
    except Exception:
        result = ""
        try:
            for part, enc in decode_header(value):
                result += _decode_bytes(part, enc) if isinstance(part, bytes) else part
        except Exception:
            result = value
    return re.sub(r"\s*[\r\n]+\s*", " ", result).strip()


def normalize_subject(subject: str) -> str:
    s = subject or ""
    while True:
        new = _PREFIX_RE.sub("", s, count=1)
        if new == s:
            break
        s = new
    return re.sub(r"\s+", " ", s).strip().lower()[:500]


def parse_addresses(msg: Message, header: str) -> list[Address]:
    values = [str(v) for v in msg.get_all(header, [])]
    result = []
    for name, addr in getaddresses(values):
        addr = (addr or "").strip().lower()
        if not addr or "@" not in addr:
            continue
        result.append(Address(name=decode_mime(name).strip('" '), email=addr))
    return result


def decode_part(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        return ""
    return _decode_bytes(payload, part.get_content_charset())


def html_to_text(html: str) -> str:
    """HTML (в т.ч. Outlook) -> читаемый текст."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["style", "script", "head", "title", "meta", "xml"]):
        tag.decompose()
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for tag in soup.find_all(["p", "div", "tr", "li", "table", "h1", "h2", "h3", "h4"]):
        tag.append("\n")
    text = soup.get_text(separator="")
    return clean_text(text)


def clean_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def split_quoted(text: str) -> str:
    """Возвращает только новую часть письма — без цитируемой истории переписки."""
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if any(p.match(line) for p in _QUOTE_MARKERS):
            return "\n".join(lines[:i]).strip()
        if _FROM_LINE.match(line):
            window = lines[i + 1:i + 6]
            if sum(1 for w in window if _HEADER_LINE.match(w)) >= 2:
                return "\n".join(lines[:i]).strip()
        if line.startswith(">"):
            rest = [ln for ln in lines[i:] if ln.strip()]
            if all(ln.lstrip().startswith(">") for ln in rest):
                return "\n".join(lines[:i]).strip()
    return text.strip()


def safe_filename(name: str, default: str = "attachment") -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name or "").strip(" .")
    if not name:
        name = default
    if len(name) > 150:
        stem, dot, ext = name.rpartition(".")
        name = (stem[:140] + "." + ext[:9]) if dot else name[:150]
    return name


def _parse_date(msg: Message) -> datetime:
    try:
        dt = parsedate_to_datetime(msg.get("Date"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=dt_timezone.utc)
        return dt
    except Exception:
        return datetime.now(dt_timezone.utc)


def _importance(msg: Message) -> str:
    imp = (msg.get("Importance") or msg.get("X-MSMail-Priority") or "").strip().lower()
    prio = (msg.get("X-Priority") or "").strip()[:1]
    if imp == "high" or prio in ("1", "2"):
        return "high"
    if imp == "low" or prio in ("4", "5"):
        return "low"
    return "normal"


def _walk(part: Message):
    """Обход MIME-дерева; вложенные письма (message/rfc822) не раскрываем."""
    if part.get_content_type() == "message/rfc822":
        yield "rfc822", part
        return
    if part.is_multipart():
        for sub in part.get_payload():
            yield from _walk(sub)
    else:
        yield "leaf", part


def parse_message(raw: bytes) -> ParsedMessage:
    msg = email.message_from_bytes(raw)

    text_parts, html_parts = [], []
    attachments: list[ParsedAttachment] = []
    has_calendar = has_rfc822 = False

    for node_type, part in _walk(msg):
        if node_type == "rfc822":
            has_rfc822 = True
            inner = part.get_payload()
            inner = inner[0] if isinstance(inner, list) and inner else inner
            data = inner.as_bytes() if isinstance(inner, Message) else (part.get_payload(decode=True) or b"")
            subj = decode_mime(inner.get("Subject")) if isinstance(inner, Message) else ""
            filename = part.get_filename()
            name = decode_mime(filename) if filename else f"{subj or 'message'}.eml"
            attachments.append(ParsedAttachment(safe_filename(name, "message.eml"), "message/rfc822", data))
            continue

        ctype = part.get_content_type()
        disposition = (part.get_content_disposition() or "").lower()
        filename = part.get_filename()
        content_id = (part.get("Content-ID") or "").strip().strip("<>")

        if ctype == "text/calendar":
            has_calendar = True
            payload = part.get_payload(decode=True) or b""
            attachments.append(ParsedAttachment(safe_filename(decode_mime(filename) if filename else "invite.ics"),
                                                ctype, payload))
            continue

        if filename or disposition == "attachment" or (content_id and not ctype.startswith("text/")):
            payload = part.get_payload(decode=True) or b""
            if filename:
                name = decode_mime(filename)
            else:
                ext = mimetypes.guess_extension(ctype) or ".bin"
                name = f"{content_id.split('@')[0] or 'attachment'}{ext}"
            attachments.append(ParsedAttachment(safe_filename(name), ctype, payload, content_id,
                                                is_inline=disposition == "inline"))
            continue

        if ctype == "text/plain":
            text = decode_part(part)
            if text.strip():
                text_parts.append(text)
        elif ctype == "text/html":
            html = decode_part(part)
            if html.strip():
                html_parts.append(html)

    html = "\n".join(html_parts)
    # Встроенная картинка = на неё есть ссылка cid: в HTML (подписи Outlook image001.png и т.п.)
    html_lower = html.lower()
    for att in attachments:
        if att.content_id:
            att.is_inline = f"cid:{att.content_id.lower()}" in html_lower

    if text_parts:
        text = clean_text("\n".join(text_parts))
    elif html:
        text = html_to_text(html)
    else:
        text = ""

    subject = decode_mime(msg.get("Subject"))
    new_text = split_quoted(text)

    message_id = (msg.get("Message-ID") or "").strip()
    if not message_id:
        message_id = f"<generated-{hashlib.sha1(raw).hexdigest()}@mailbot>"
    in_reply_to = " ".join(re.findall(r"<[^>]+>", msg.get("In-Reply-To") or ""))[:512]
    references = re.findall(r"<[^>]+>", msg.get("References") or "")

    auto_submitted = (msg.get("Auto-Submitted") or "no").strip().lower()
    if auto_submitted != "no" or _AUTO_SUBJECT.match(subject):
        kind = "auto"
    elif has_calendar:
        kind = "calendar"
    elif FORWARD_START.match(subject) or has_rfc822 or (not new_text and _FORWARD_BODY_MARKER.search(text)):
        kind = "forward"
    elif REPLY_START.match(subject) or in_reply_to:
        kind = "reply"
    else:
        kind = "new"

    from_list = parse_addresses(msg, "From")
    return ParsedMessage(
        message_id=message_id[:512],
        in_reply_to=in_reply_to.split(" ")[0] if in_reply_to else "",
        references=references,
        subject=subject,
        thread_topic=decode_mime(msg.get("Thread-Topic")),
        from_=from_list[0] if from_list else None,
        reply_to=parse_addresses(msg, "Reply-To"),
        to=parse_addresses(msg, "To"),
        cc=parse_addresses(msg, "Cc"),
        date=_parse_date(msg),
        text=text,
        html=html,
        new_text=new_text,
        kind=kind,
        importance=_importance(msg),
        size=len(raw),
        attachments=attachments,
    )
