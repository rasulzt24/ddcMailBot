import uuid
from datetime import timedelta

from django.core.files.base import ContentFile
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from ..models import Contact, MailAccount, MailAttachment, MailMessage, MailThread, MessageRecipient
from .parser import Address, ParsedMessage, normalize_subject

THREAD_SUBJECT_WINDOW = timedelta(days=60)


def resolve_thread(account: MailAccount, parsed: ParsedMessage) -> MailThread:
    """Цепочка: сначала по In-Reply-To/References, затем (для ответов) по теме."""
    ids = [i for i in [*parsed.references, parsed.in_reply_to] if i]
    if ids:
        parent = (MailMessage.objects
                  .filter(account=account, message_id__in=ids, thread__isnull=False)
                  .select_related("thread").order_by("-date").first())
        if parent:
            return parent.thread

    # Письмо могло прийти раньше родителя — ищем тех, кто ссылается на нас
    child = (MailMessage.objects
             .filter(account=account, in_reply_to=parsed.message_id, thread__isnull=False)
             .select_related("thread").first())
    if child:
        return child.thread

    subject = normalize_subject(parsed.thread_topic or parsed.subject)
    if subject and parsed.kind in ("reply", "forward", "auto"):
        thread = (MailThread.objects
                  .filter(account=account, subject=subject,
                          last_message_at__gte=parsed.date - THREAD_SUBJECT_WINDOW)
                  .order_by("-last_message_at").first())
        if thread:
            return thread
    return MailThread.objects.create(account=account, subject=subject, last_message_at=parsed.date)


def touch_contacts(account: MailAccount, addresses: list[Address], sent: bool = False) -> None:
    own = account.email.lower()
    now = timezone.now()
    seen = set()
    for addr in addresses:
        email = addr.email.lower()
        if not email or email == own or email in seen:
            continue
        seen.add(email)
        contact, created = Contact.objects.get_or_create(account=account, email=email, defaults={"name": addr.name})
        updates = {"seen_count": F("seen_count") + 1}
        if sent:
            updates = {"sent_count": F("sent_count") + 1, "last_used_at": now}
        if addr.name and not contact.name:
            updates["name"] = addr.name
        Contact.objects.filter(pk=contact.pk).update(**updates)


def store_message(account: MailAccount, parsed: ParsedMessage, raw: bytes, *, direction: str,
                  folder: str, uid: int | None = None, uidvalidity: int | None = None,
                  is_read: bool = False, notify: bool = True, store_raw: bool = True) -> MailMessage:
    with transaction.atomic():
        thread = resolve_thread(account, parsed)
        message = MailMessage(
            account=account,
            thread=thread,
            direction=direction,
            folder=folder,
            uid=uid,
            uidvalidity=uidvalidity,
            message_id=parsed.message_id,
            in_reply_to=parsed.in_reply_to,
            references=" ".join(parsed.references),
            kind=parsed.kind,
            importance=parsed.importance,
            from_name=parsed.from_.name[:255] if parsed.from_ else "",
            from_email=parsed.from_.email if parsed.from_ else "",
            reply_to=", ".join(a.formatted() for a in parsed.reply_to)[:512],
            subject=parsed.subject,
            date=parsed.date,
            body_text=parsed.text,
            body_html=parsed.html,
            body_new_text=parsed.new_text,
            is_read=is_read,
            size=parsed.size,
            notify=notify,
        )
        if store_raw:
            message.raw.save(f"{uuid.uuid4().hex}.eml", ContentFile(raw), save=False)
        message.save()

        MessageRecipient.objects.bulk_create(
            [MessageRecipient(message=message, kind="to", name=a.name[:255], email=a.email) for a in parsed.to]
            + [MessageRecipient(message=message, kind="cc", name=a.name[:255], email=a.email) for a in parsed.cc]
        )
        for att in parsed.attachments:
            obj = MailAttachment(message=message, filename=att.filename, content_type=att.content_type,
                                 size=len(att.payload), content_id=att.content_id[:255], is_inline=att.is_inline)
            obj.file.save(att.filename, ContentFile(att.payload), save=False)
            obj.save()

        if not thread.last_message_at or parsed.date > thread.last_message_at:
            MailThread.objects.filter(pk=thread.pk).update(last_message_at=parsed.date)

        if direction == MailMessage.Direction.INCOMING:
            touch_contacts(account, ([parsed.from_] if parsed.from_ else []) + parsed.to + parsed.cc)
        else:
            touch_contacts(account, parsed.to + parsed.cc, sent=True)
    return message
