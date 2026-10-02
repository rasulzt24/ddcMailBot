import html
import logging
import mimetypes
import re
import smtplib
from datetime import timedelta
from email.message import EmailMessage
from email.utils import formataddr, formatdate, getaddresses, make_msgid

from bs4 import BeautifulSoup
from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.accounts.models import TelegramUser

from ..models import Contact, MailAccount, MailMessage, OutgoingAttachment, OutgoingMessage
from . import events, smtp
from .imap import ImapSession
from .parser import FORWARD_START, REPLY_START, format_address, parse_message, safe_filename
from .storage import store_message

logger = logging.getLogger(__name__)

EMAIL_RE = re.compile(r"^[^@\s<>\"',;]+@[^@\s<>\"',;]+\.[^@\s<>\"',;]+$")
MODE = OutgoingMessage.Mode


# ---------- Получатели ----------

def split_addresses(value: str) -> list[tuple[str, str]]:
    return [(n.strip(), e.strip().lower()) for n, e in getaddresses([value]) if e]


def resolve_recipients(account: MailAccount, text: str) -> tuple[list[str], list[str], dict[str, list[Contact]]]:
    """Разбирает ввод пользователя: адреса и/или имена из адресной книги.

    Возвращает (распознанные адреса, нераспознанные токены, варианты для неоднозначных).
    """
    resolved, unknown, ambiguous = [], [], {}
    for token in re.split(r"[,;\n]+", text):
        token = token.strip()
        if not token:
            continue
        if "@" in token:
            pairs = split_addresses(token)
            if pairs and EMAIL_RE.match(pairs[0][1]):
                name, email = pairs[0]
                resolved.append(format_address(name, email))
            else:
                unknown.append(token)
            continue
        matches = list(
            Contact.objects.filter(account=account)
            .filter(Q(name__icontains=token) | Q(email__istartswith=token))
            .order_by("-sent_count", "-seen_count")[:6]
        )
        if len(matches) == 1:
            resolved.append(contact_address(matches[0]))
        elif matches:
            ambiguous[token] = matches
        else:
            unknown.append(token)
    return resolved, unknown, ambiguous


def contact_address(contact: Contact) -> str:
    return format_address(contact.name, contact.email)


def frequent_contacts(account: MailAccount, limit: int = 6) -> list[Contact]:
    return list(Contact.objects.filter(account=account).order_by("-sent_count", "-seen_count", "-last_used_at")[:limit])


def emails_of(addresses: list[str]) -> list[str]:
    return [e for _, e in getaddresses(addresses) if e]


def dedupe(addresses: list[str], exclude: set[str] = frozenset()) -> list[str]:
    seen, result = set(exclude), []
    for addr in addresses:
        pairs = getaddresses([addr])
        email = pairs[0][1].lower() if pairs else ""
        if email and email not in seen:
            seen.add(email)
            result.append(addr)
    return result


# ---------- Черновики ----------

def _prefixed(prefix: str, subject: str) -> str:
    pattern = REPLY_START if prefix == "RE:" else FORWARD_START
    return subject if pattern.match(subject or "") else f"{prefix} {subject}".strip()


def draft_for(account: MailAccount, mode: str, source: MailMessage | None) -> dict:
    """Начальные поля черновика для нового письма / ответа / пересылки."""
    draft = {"mode": mode, "source_id": source.pk if source else None, "to": [], "cc": [],
             "subject": "", "body": "", "files": [], "include_attachments": mode == MODE.FORWARD}
    if not source:
        return draft
    own = {account.email.lower()}
    author = source.reply_to or (format_address(source.from_name, source.from_email) if source.from_email else "")
    recipients = list(source.recipients.all())

    if mode in (MODE.REPLY, MODE.REPLY_ALL):
        draft["subject"] = _prefixed("RE:", source.subject)
        if source.direction == MailMessage.Direction.OUTGOING:
            # Ответ на собственное письмо — адресуем тем же получателям
            draft["to"] = dedupe([format_address(r.name, r.email) for r in recipients if r.kind == "to"], own)
        else:
            draft["to"] = dedupe([author], own) if author else []
        if mode == MODE.REPLY_ALL:
            to_emails = own | {e.lower() for e in emails_of(draft["to"])}
            draft["to"] += dedupe([format_address(r.name, r.email) for r in recipients if r.kind == "to"], to_emails)
            to_emails |= {e.lower() for e in emails_of(draft["to"])}
            draft["cc"] = dedupe([format_address(r.name, r.email) for r in recipients if r.kind == "cc"], to_emails)
    elif mode == MODE.FORWARD:
        draft["subject"] = _prefixed("FW:", source.subject)
    return draft


def reply_all_possible(message: MailMessage, own_email: str) -> bool:
    others = {r.email.lower() for r in message.recipients.all()} - {own_email.lower(), message.from_email.lower()}
    return bool(others)


# ---------- Тело письма ----------

def _text_to_html(text: str) -> str:
    return html.escape(text).replace("\n", "<br>\n")


def _source_html_body(source: MailMessage) -> str:
    if source.body_html:
        soup = BeautifulSoup(source.body_html, "html.parser")
        for tag in soup(["script", "title", "meta"]):
            tag.decompose()
        body = soup.body
        return body.decode_contents() if body else str(soup)
    return _text_to_html(source.body_text)


def _quote_header(source: MailMessage, as_html: bool) -> str:
    recipients = list(source.recipients.all())
    to = "; ".join(format_address(r.name, r.email) for r in recipients if r.kind == "to")
    cc = "; ".join(format_address(r.name, r.email) for r in recipients if r.kind == "cc")
    date = timezone.localtime(source.date).strftime("%d.%m.%Y %H:%M")
    rows = [("От", source.from_display), ("Отправлено", date), ("Кому", to)]
    if cc:
        rows.append(("Копия", cc))
    rows.append(("Тема", source.subject))
    if as_html:
        return "<br>\n".join(f"<b>{k}:</b> {html.escape(v)}" for k, v in rows)
    return "\n".join(f"{k}: {v}" for k, v in rows)


def build_bodies(user: TelegramUser, mode: str, source: MailMessage | None, body: str) -> tuple[str, str]:
    text = body.strip()
    html_body = f'<div style="font-family:Calibri,Arial,sans-serif;font-size:11pt">{_text_to_html(text)}'
    if user.signature:
        text += f"\n\n--\n{user.signature}"
        html_body += f"<br><br>--<br>{_text_to_html(user.signature)}"
    html_body += "</div>"

    if source:
        title = "Пересылаемое сообщение" if mode == MODE.FORWARD else "Исходное сообщение"
        text += f"\n\n-------- {title} --------\n{_quote_header(source, False)}\n\n{source.body_text}"
        html_body += (
            f'<br><hr style="border:none;border-top:1px solid #E1E1E1">'
            f'<div style="font-family:Calibri,Arial,sans-serif;font-size:11pt">{_quote_header(source, True)}</div><br>'
            f"{_source_html_body(source)}"
        )
    return text, f"<html><head><meta charset=\"utf-8\"></head><body>{html_body}</body></html>"


# ---------- Создание исходящего ----------

def create_outgoing(user: TelegramUser, account: MailAccount, draft: dict,
                    files: list[tuple[str, str, bytes]]) -> OutgoingMessage:
    source = MailMessage.objects.filter(pk=draft.get("source_id"), account=account).first()
    text, html_body = build_bodies(user, draft["mode"], source, draft.get("body", ""))
    with transaction.atomic():
        outgoing = OutgoingMessage.objects.create(
            account=account, created_by=user, mode=draft["mode"], source_message=source,
            to=draft["to"], cc=draft.get("cc", []), subject=draft.get("subject", ""),
            body_text=text, body_html=html_body,
        )
        for name, mime, data in files:
            name = safe_filename(name)
            att = OutgoingAttachment(outgoing=outgoing, filename=name, content_type=mime, size=len(data))
            att.file.save(name, ContentFile(data), save=False)
            att.save()
        if source and draft.get("include_attachments"):
            outgoing.forward_attachments.set(source.attachments.filter(is_inline=False))
    events.notify_worker("outgoing", outgoing_id=outgoing.pk)
    return outgoing


# ---------- Сборка MIME и отправка ----------

def _split_mime(content_type: str, filename: str) -> tuple[str, str]:
    ctype = content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    maintype, _, subtype = ctype.partition("/")
    return maintype, subtype or "octet-stream"


def build_mime(outgoing: OutgoingMessage) -> EmailMessage:
    account = outgoing.account
    source = outgoing.source_message
    msg = EmailMessage()
    msg["From"] = formataddr((account.display_name, account.email)) if account.display_name else account.email
    msg["To"] = ", ".join(outgoing.to)
    if outgoing.cc:
        msg["Cc"] = ", ".join(outgoing.cc)
    msg["Subject"] = outgoing.subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = outgoing.smtp_message_id or make_msgid(domain=account.domain)
    if source and outgoing.mode in (MODE.REPLY, MODE.REPLY_ALL):
        msg["In-Reply-To"] = source.message_id
        msg["References"] = " ".join([*source.references.split(), source.message_id]).strip()

    msg.set_content(outgoing.body_text)
    msg.add_alternative(outgoing.body_html, subtype="html")

    if source and source.body_html:
        # Картинки из подписи/текста исходного письма, чтобы цитата не «сломалась»
        html_part = msg.get_body(preferencelist=("html",))
        for att in source.attachments.filter(is_inline=True).exclude(content_id=""):
            with att.file.open("rb") as f:
                data = f.read()
            maintype, subtype = _split_mime(att.content_type, att.filename)
            html_part.add_related(data, maintype=maintype, subtype=subtype,
                                  cid=f"<{att.content_id}>", filename=att.filename)

    for att in list(outgoing.attachments.all()) + list(outgoing.forward_attachments.all()):
        with att.file.open("rb") as f:
            data = f.read()
        maintype, subtype = _split_mime(att.content_type, att.filename)
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=att.filename)
    return msg


def _save_to_sent(account: MailAccount, raw: bytes) -> None:
    with ImapSession.for_account(account) as imap:
        folder = account.sent_folder or imap.find_sent_folder()
        if not folder:
            logger.warning("%s: Sent folder not found, skip APPEND", account)
            return
        if folder != account.sent_folder:
            MailAccount.objects.filter(pk=account.pk).update(sent_folder=folder)
        imap.append(folder, raw)


def send_outgoing(outgoing_id: int) -> None:
    outgoing = OutgoingMessage.objects.select_related("account", "source_message").get(pk=outgoing_id)
    account = outgoing.account
    if not outgoing.smtp_message_id:
        outgoing.smtp_message_id = make_msgid(domain=account.domain)
    outgoing.attempts += 1
    outgoing.save(update_fields=["smtp_message_id", "attempts"])

    try:
        msg = build_mime(outgoing)
        recipients = emails_of(outgoing.to + outgoing.cc + outgoing.bcc)
        smtp.send(account, msg, recipients)
    except smtplib.SMTPAuthenticationError as e:
        from .sync import mark_needs_reauth
        mark_needs_reauth(account, f"SMTP: {e}")
        _fail(outgoing, "Пароль от почты не подходит (возможно, его сменили). "
                        "Обновите пароль: «📬 Моя почта» → «🔑 Сменить пароль», затем отправьте письмо заново.",
              final=True)
        return
    except smtplib.SMTPRecipientsRefused as e:
        _fail(outgoing, f"{type(e).__name__}: {e}", final=True)
        return
    except Exception as e:
        # 5xx — постоянная ошибка, 4xx/сеть — пробуем ещё раз позже
        permanent = isinstance(e, smtplib.SMTPResponseException) and e.smtp_code >= 500
        final = permanent or outgoing.attempts >= settings.MAIL_SEND_MAX_ATTEMPTS
        _fail(outgoing, f"{type(e).__name__}: {e}", final=final)
        return

    raw = msg.as_bytes()
    if settings.MAIL_SAVE_TO_SENT:
        try:
            _save_to_sent(account, raw)
        except Exception as e:
            logger.warning("%s: APPEND to Sent failed: %s", account, e)

    sent_message = None
    try:
        sent_message = store_message(account, parse_message(raw), raw, direction=MailMessage.Direction.OUTGOING,
                                     folder="SENT", is_read=True, notify=False,
                                     store_raw=settings.MAIL_STORE_RAW)
    except Exception:
        logger.exception("%s: can't store sent copy", account)

    OutgoingMessage.objects.filter(pk=outgoing.pk).update(
        status=OutgoingMessage.Status.SENT, sent_at=timezone.now(), error="", sent_message=sent_message)
    events.notify_bot("outgoing_done", outgoing_id=outgoing.pk)
    logger.info("%s: sent outgoing #%s to %s", account, outgoing.pk, recipients)


def _fail(outgoing: OutgoingMessage, error: str, final: bool) -> None:
    logger.warning("outgoing #%s failed (final=%s): %s", outgoing.pk, final, error)
    OutgoingMessage.objects.filter(pk=outgoing.pk).update(
        status=OutgoingMessage.Status.FAILED if final else OutgoingMessage.Status.QUEUED,
        error=error[:2000],
        next_attempt_at=None if final else timezone.now() + timedelta(seconds=30 * outgoing.attempts),
    )
    if final:
        events.notify_bot("outgoing_done", outgoing_id=outgoing.pk)
