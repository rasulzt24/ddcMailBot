"""Доступ к БД для бота. Всё синхронное ORM-взаимодействие — здесь, через sync_to_async."""
import time
from dataclasses import dataclass
from datetime import timedelta

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from asgiref.sync import sync_to_async
from django.db import close_old_connections
from django.db.models import Count, Exists, OuterRef, Q, Subquery
from django.db.models.functions import Length
from django.utils import timezone

from apps.accounts.models import TelegramUser
from apps.accounts.services import get_or_register
from apps.mail.models import (Contact, MailAccount, MailAttachment, MailMessage, MessageRecipient,
                              OutgoingMessage, TelegramNotification)
from apps.mail.services import compose, events

from . import keyboards, render

PAGE_SIZE = 8

close_db = sync_to_async(close_old_connections)
_register_user = sync_to_async(get_or_register)

# Кэш пользователей и ящиков в памяти процесса бота: БД удалённая (~200 мс на запрос),
# а пользователь нужен на каждое нажатие кнопки
CACHE_TTL = 60
_users: dict[int, tuple[float, TelegramUser]] = {}
_accounts: dict[int, tuple[float, "MailAccount | None"]] = {}


async def register_user(telegram_id: int, username: str, full_name: str) -> tuple[TelegramUser, bool]:
    hit = _users.get(telegram_id)
    if hit and time.monotonic() - hit[0] < CACHE_TTL \
            and hit[1].username == username and hit[1].full_name == full_name:
        return hit[1], False
    user, created = await _register_user(telegram_id, username, full_name)
    _users[telegram_id] = (time.monotonic(), user)
    return user, created


def _remember_user(user: TelegramUser) -> None:
    _users[user.telegram_id] = (time.monotonic(), user)


def _forget_account(user_id: int) -> None:
    _accounts.pop(user_id, None)


@dataclass
class AttachmentInfo:
    id: int
    filename: str
    path: str
    size: int
    tg_file_id: str


@dataclass
class Card:
    message_id: int
    text: str
    markup: InlineKeyboardMarkup
    attachments: list[AttachmentInfo]
    auto_send_attachments: bool


def _local_path(field) -> str:
    """Путь на диске, если хранилище локальное; для S3/MinIO — пусто (читаем байты через storage)."""
    try:
        return field.path
    except NotImplementedError:
        return ""


def _attachment_info(a: MailAttachment) -> AttachmentInfo:
    return AttachmentInfo(a.pk, a.filename, _local_path(a.file), a.size, a.tg_file_id)


@sync_to_async
def read_attachment(attachment_id: int) -> bytes:
    att = MailAttachment.objects.only("file").get(pk=attachment_id)
    with att.file.open("rb") as f:
        return f.read()


def _account(user: TelegramUser) -> MailAccount | None:
    hit = _accounts.get(user.pk)
    if hit and time.monotonic() - hit[0] < CACHE_TTL:
        return hit[1]
    account = MailAccount.objects.filter(owner=user).order_by("created_at").first()
    _accounts[user.pk] = (time.monotonic(), account)
    return account


def _own_message(message_id: int, user: TelegramUser):
    # body_html (десятки КБ) нужен только для HTML-версии — грузим его отдельно
    return (MailMessage.objects.select_related("account")
            .defer("body_html")
            .prefetch_related("recipients", "attachments")
            .get(pk=message_id, account__owner=user))


# ---------- Пользователи и ящики ----------

@sync_to_async
def get_account(user: TelegramUser) -> MailAccount | None:
    _forget_account(user.pk)
    return _account(user)


@sync_to_async
def save_account(user: TelegramUser, email: str, login: str, password: str) -> MailAccount:
    account = MailAccount.objects.filter(owner=user, email=email).first() or MailAccount(owner=user, email=email)
    account.login = login
    account.password = password
    if not account.display_name:
        account.display_name = user.full_name
    account.is_active = True
    account.needs_reauth = False
    account.reauth_notified_at = None
    account.error_count = 0
    account.last_error = ""
    account.last_sync_at = None
    account.save()
    _forget_account(user.pk)
    events.notify_worker("account", account_id=account.pk)
    return account


@sync_to_async
def login_style(domain: str) -> str | None:
    """Какой формат логина уже сработал для этого домена: полный e-mail или часть до @."""
    acc = MailAccount.objects.filter(email__iendswith=f"@{domain}", needs_reauth=False).first()
    if not acc:
        return None
    return "email" if "@" in acc.login else "local"


@sync_to_async
def update_account(account_id: int, **fields) -> None:
    MailAccount.objects.filter(pk=account_id).update(**fields)
    _accounts.clear()
    events.notify_worker("account", account_id=account_id)


@sync_to_async
def delete_account(account_id: int) -> None:
    MailAccount.objects.filter(pk=account_id).delete()
    _accounts.clear()


@sync_to_async
def account_stats(account: MailAccount) -> dict:
    qs = MailMessage.objects.filter(account=account)
    return {
        "incoming": qs.filter(direction=MailMessage.Direction.INCOMING).count(),
        "outgoing": qs.filter(direction=MailMessage.Direction.OUTGOING).count(),
        "unread": qs.filter(direction=MailMessage.Direction.INCOMING, is_read=False).count(),
        "contacts": Contact.objects.filter(account=account).count(),
    }


@sync_to_async
def update_user(user_id: int, **fields) -> TelegramUser:
    TelegramUser.objects.filter(pk=user_id).update(**fields)
    user = TelegramUser.objects.get(pk=user_id)
    _remember_user(user)
    return user


# ---------- Карточки писем ----------

def _build_card(m: MailMessage, user: TelegramUser, title: str) -> Card:
    thread_total, thread_pos = 1, 1
    if m.thread_id:
        counts = MailMessage.objects.filter(thread_id=m.thread_id).aggregate(
            total=Count("id"), pos=Count("id", filter=Q(date__lte=m.date)))
        thread_total, thread_pos = counts["total"], counts["pos"]
    attachments = list(m.attachments.all())
    files = [a for a in attachments if not a.is_inline]
    markup = keyboards.message_actions(
        m.pk,
        has_files=bool(files),
        has_inline=any(a.is_inline for a in attachments),
        can_reply_all=compose.reply_all_possible(m, m.account.email),
        in_thread=thread_total > 1,
        is_read=m.is_read,
        incoming=m.direction == MailMessage.Direction.INCOMING and m.uid is not None,
    )
    return Card(m.pk, render.message_card(m, title, thread_pos, thread_total), markup,
                [_attachment_info(a) for a in files], user.send_attachments)


@sync_to_async
def card_for(message_id: int, user: TelegramUser, title: str = "📨 <b>Письмо</b>") -> Card:
    return _build_card(_own_message(message_id, user), user, title)


@sync_to_async
def notification_card(message_id: int) -> tuple[TelegramUser, Card]:
    m = (MailMessage.objects.select_related("account", "account__owner").defer("body_html")
         .prefetch_related("recipients", "attachments").get(pk=message_id))
    user = m.account.owner
    return user, _build_card(m, user, "📩 <b>Новое письмо</b>")


@sync_to_async
def record_notification(message_id: int, user: TelegramUser, chat_id: int, tg_message_id: int) -> None:
    TelegramNotification.objects.create(message_id=message_id, user=user, chat_id=chat_id,
                                        tg_message_id=tg_message_id)


@sync_to_async
def cards_to_refresh(message_ids: list[int]) -> list[tuple[int, TelegramUser, int, int]]:
    """Карточки в Telegram, где нужно обновить кнопки (например, письмо прочитали в Outlook)."""
    return [(n.message_id, n.user, n.chat_id, n.tg_message_id)
            for n in TelegramNotification.objects.select_related("user").filter(message_id__in=message_ids)]


@sync_to_async
def card_delivered(message_id: int) -> bool:
    return TelegramNotification.objects.filter(message_id=message_id).exists()


@sync_to_async
def message_by_telegram(chat_id: int, tg_message_id: int, user: TelegramUser) -> int | None:
    n = (TelegramNotification.objects.filter(chat_id=chat_id, tg_message_id=tg_message_id,
                                             message__account__owner=user).first())
    return n.message_id if n else None


@sync_to_async
def full_text(message_id: int, user: TelegramUser) -> tuple[str, str, bool, bool]:
    m = (MailMessage.objects.filter(pk=message_id, account__owner=user)
         .annotate(html_len=Length("body_html")).only("subject", "body_text", "raw").get())
    return m.subject, m.body_text, bool(m.html_len), bool(m.raw)


@sync_to_async
def html_body(message_id: int, user: TelegramUser) -> tuple[str, str]:
    return tuple(MailMessage.objects.filter(pk=message_id, account__owner=user)
                 .values_list("subject", "body_html").get())


@sync_to_async
def raw_source(message_id: int, user: TelegramUser):
    """Оригинал письма: сохранённый файл или (account, folder, uid) для загрузки с IMAP."""
    m = (MailMessage.objects.select_related("account").only("raw", "subject", "uid", "folder", "account")
         .get(pk=message_id, account__owner=user))
    if m.raw:
        with m.raw.open("rb") as f:
            return m.subject, f.read(), None
    if m.uid:
        return m.subject, None, (m.account, m.folder, m.uid)
    return m.subject, None, None


@sync_to_async
def attachments_of(message_id: int, user: TelegramUser, inline: bool) -> list[AttachmentInfo]:
    return [_attachment_info(a) for a in MailAttachment.objects.filter(
        message_id=message_id, message__account__owner=user, is_inline=inline).order_by("id")]


@sync_to_async
def cache_file_id(attachment_id: int, file_id: str) -> None:
    MailAttachment.objects.filter(pk=attachment_id).update(tg_file_id=file_id)


@sync_to_async
def thread_view(message_id: int, user: TelegramUser) -> tuple[str, InlineKeyboardMarkup]:
    m = _own_message(message_id, user)
    messages = [m]
    if m.thread_id:
        messages = list(MailMessage.objects.filter(thread_id=m.thread_id)
                        .only("id", "date", "direction", "kind", "from_name", "from_email", "subject",
                              "body_new_text", "body_text")
                        .annotate(has_files=Exists(MailAttachment.objects.filter(message=OuterRef("pk"),
                                                                                  is_inline=False)))
                        .order_by("date", "id"))
    else:
        m.has_files = any(not x.is_inline for x in m.attachments.all())
    text = render.thread_history(messages, m.account.email)
    kb = InlineKeyboardBuilder()
    # Кнопки от новых к старым; номер = порядковый номер письма в цепочке
    for i, msg in list(enumerate(messages, 1))[::-1][:15]:
        kb.button(text=f"{'📤' if msg.direction == 'out' else '📥'} {i}", callback_data=f"m:o:{msg.pk}")
    kb.adjust(5)
    return text, kb.as_markup()


@sync_to_async
def mark_read_local(message_id: int, user: TelegramUser, seen: bool) -> tuple[MailAccount, int, str] | None:
    m = MailMessage.objects.select_related("account").only("uid", "folder", "account").get(
        pk=message_id, account__owner=user)
    MailMessage.objects.filter(pk=m.pk).update(is_read=seen, read_changed_at=timezone.now())
    return (m.account, m.uid, m.folder) if m.uid else None


@sync_to_async
def is_read(message_id: int, user: TelegramUser) -> bool:
    return MailMessage.objects.values_list("is_read", flat=True).get(pk=message_id, account__owner=user)


# ---------- Списки ----------

# Для списка тела писем не нужны — это десятки КБ на письмо по медленному каналу
LIST_FIELDS = ("id", "date", "subject", "from_name", "from_email", "is_read", "direction")


def _list_markup(messages, own_email: str, prefix: str, page: int, has_next: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for m in messages:
        mark = "" if m.direction == "out" else ("▫️" if m.is_read else "🔵")
        who = render.short_person(m.from_name, m.from_email, own_email)
        if m.direction == "out":
            who = "→ " + (render.short_person(m.to_name or "", m.to_email) if m.to_email else "?")
        clip = "📎" if m.has_files else ""
        label = f"{mark}{clip} {render.fmt_date(m.date, short=True)} · {who} · {m.subject or '(без темы)'}"
        kb.button(text=label[:64], callback_data=f"m:o:{m.pk}")
    for btn in keyboards.pager(prefix, page, has_next):
        kb.add(btn)
    kb.adjust(*([1] * len(messages)), 2)
    return kb.as_markup()


@sync_to_async
def message_list(user: TelegramUser, direction: str, page: int, query: str = "") -> tuple[str, InlineKeyboardMarkup | None]:
    account = _account(user)
    if not account:
        return "📭 Почта не подключена. Нажмите «📬 Моя почта».", None
    qs = MailMessage.objects.filter(account=account)
    if query:
        in_recipients = MessageRecipient.objects.filter(message=OuterRef("pk")).filter(
            Q(email__icontains=query) | Q(name__icontains=query))
        qs = qs.filter(Q(subject__icontains=query) | Q(from_email__icontains=query) | Q(from_name__icontains=query)
                       | Q(body_text__icontains=query) | Exists(in_recipients))
        title = f"🔎 Результаты поиска «{render.esc(query)}»"
        prefix = "srch"
    else:
        qs = qs.filter(direction=direction)
        title = "📥 <b>Входящие</b>" if direction == "in" else "📤 <b>Отправленные</b>"
        prefix = f"box:{direction}"

    first_to = MessageRecipient.objects.filter(message=OuterRef("pk"), kind="to").order_by("id")
    qs = (qs.only(*LIST_FIELDS)
          .annotate(has_files=Exists(MailAttachment.objects.filter(message=OuterRef("pk"), is_inline=False)),
                    to_name=Subquery(first_to.values("name")[:1]),
                    to_email=Subquery(first_to.values("email")[:1]))
          .order_by("-date", "-id"))  # новые сверху
    items = list(qs[page * PAGE_SIZE:(page + 1) * PAGE_SIZE + 1])
    has_next = len(items) > PAGE_SIZE
    items = items[:PAGE_SIZE]
    if not items:
        return f"{title}\n\nНичего не найдено.", None
    unread = ""
    if direction == "in" and not query:
        n = MailMessage.objects.filter(account=account, direction="in", is_read=False).count()
        unread = f" · непрочитанных: {n}"
    text = f"{title}{unread}\nСтраница {page + 1} · сначала новые. Нажмите на письмо, чтобы открыть."
    if account.needs_reauth:
        text = keyboards.REAUTH_BANNER + "\n\n" + text
    return text, _list_markup(items, account.email, prefix, page, has_next)


# ---------- Составление писем ----------

@sync_to_async
def new_draft(user: TelegramUser, mode: str, source_id: int | None) -> dict:
    account = _account(user)
    source = _own_message(source_id, user) if source_id else None
    draft = compose.draft_for(account, mode, source)
    draft["source_files"] = source.attachments.filter(is_inline=False).count() if source else 0
    draft["source_subject"] = source.subject if source else ""
    return draft


@sync_to_async
def resolve(user: TelegramUser, text: str):
    account = _account(user)
    return compose.resolve_recipients(account, text)


@sync_to_async
def contact_address(user: TelegramUser, contact_id: int) -> str | None:
    c = Contact.objects.filter(pk=contact_id, account__owner=user).first()
    return compose.contact_address(c) if c else None


@sync_to_async
def frequent_contacts(user: TelegramUser, exclude: list[str]) -> list[Contact]:
    account = _account(user)
    if not account:
        return []
    excluded = {e.lower() for e in compose.emails_of(exclude)}
    return [c for c in compose.frequent_contacts(account, 10) if c.email not in excluded][:6]


@sync_to_async
def queue_outgoing(user: TelegramUser, draft: dict, files: list[tuple[str, str, bytes]]) -> int:
    account = _account(user)
    return compose.create_outgoing(user, account, draft, files).pk


# ---------- Для нотификатора ----------

@sync_to_async
def pending_notifications(limit: int = 20) -> list[int]:
    base = MailMessage.objects.filter(direction=MailMessage.Direction.INCOMING, notify=True, notified_at__isnull=True)
    # Пользователь отключил уведомления / ящик выключен — просто помечаем, чтобы потом не было лавины
    base.filter(Q(account__owner__notifications_enabled=False) | Q(account__owner__status="blocked")
                | Q(created_at__lt=timezone.now() - timedelta(days=2))).update(notified_at=timezone.now())
    return list(base.filter(account__owner__notifications_enabled=True)
                .filter(Q(account__owner__status="active") | Q(account__owner__is_superadmin=True))
                .order_by("date").values_list("id", flat=True)[:limit])


@sync_to_async
def mark_notified(message_id: int) -> None:
    MailMessage.objects.filter(pk=message_id).update(notified_at=timezone.now())


@sync_to_async
def pending_outgoing_results() -> list[tuple[int, str]]:
    result = []
    qs = (OutgoingMessage.objects.select_related("created_by")
          .filter(result_notified=False, status__in=[OutgoingMessage.Status.SENT, OutgoingMessage.Status.FAILED]))
    for o in qs[:20]:
        subject = render.esc(o.subject) or "(без темы)"
        to = render.esc(", ".join(o.to + o.cc))
        if o.status == OutgoingMessage.Status.SENT:
            text = f"✅ <b>Письмо отправлено</b>\n📌 {subject}\n📨 {to}"
        else:
            text = f"❌ <b>Не удалось отправить письмо</b>\n📌 {subject}\n📨 {to}\n\n<code>{render.esc(o.error[:500])}</code>"
        result.append((o.created_by.telegram_id, text))
        OutgoingMessage.objects.filter(pk=o.pk).update(result_notified=True)
    return result


REAUTH_REMIND_EVERY = timedelta(hours=24)


@sync_to_async
def pending_reauth() -> list[tuple[int, str, bool]]:
    """Ящики с неподходящим паролем: сразу уведомляем, затем напоминаем раз в сутки."""
    now = timezone.now()
    result = []
    qs = (MailAccount.objects.select_related("owner").filter(needs_reauth=True)
          .filter(Q(reauth_notified_at__isnull=True) | Q(reauth_notified_at__lte=now - REAUTH_REMIND_EVERY)))
    for acc in qs:
        result.append((acc.owner.telegram_id, acc.email, acc.reauth_notified_at is not None))
        MailAccount.objects.filter(pk=acc.pk).update(reauth_notified_at=now)
    _accounts.clear()
    return result


# ---------- Админ ----------

@sync_to_async
def admin_stats() -> dict:
    today = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
    users = TelegramUser.objects.values("status").annotate(n=Count("id"))
    return {
        "users": {u["status"]: u["n"] for u in users},
        "accounts": MailAccount.objects.count(),
        "accounts_err": MailAccount.objects.filter(Q(needs_reauth=True) | Q(error_count__gt=0)).count(),
        "in_today": MailMessage.objects.filter(direction="in", date__gte=today).count(),
        "out_today": OutgoingMessage.objects.filter(status="sent", sent_at__gte=today).count(),
        "out_failed": OutgoingMessage.objects.filter(status="failed", created_at__gte=today).count(),
        "queue": OutgoingMessage.objects.filter(status__in=["queued", "sending"]).count(),
    }


@sync_to_async
def users_page(page: int, size: int = 10) -> tuple[list[TelegramUser], bool]:
    items = list(TelegramUser.objects.order_by("status", "-created_at")[page * size:(page + 1) * size + 1])
    return items[:size], len(items) > size


@sync_to_async
def user_detail(user_id: int) -> tuple[TelegramUser, MailAccount | None]:
    u = TelegramUser.objects.get(pk=user_id)
    return u, MailAccount.objects.filter(owner=u).first()


@sync_to_async
def problem_accounts() -> list[MailAccount]:
    return list(MailAccount.objects.select_related("owner")
                .filter(Q(needs_reauth=True) | Q(error_count__gt=0) | Q(is_active=False))[:30])


@sync_to_async
def active_chat_ids() -> list[int]:
    return list(TelegramUser.objects.filter(Q(status="active") | Q(is_superadmin=True))
                .values_list("telegram_id", flat=True))
