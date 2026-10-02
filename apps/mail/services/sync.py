import imaplib
import logging
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from ..models import MailAccount, MailMessage
from . import events
from .imap import ImapAuthError, ImapSession
from .parser import parse_message
from .storage import store_message

logger = logging.getLogger(__name__)

INBOX = "INBOX"
IN, OUT = MailMessage.Direction.INCOMING, MailMessage.Direction.OUTGOING


def _import_uid(account: MailAccount, imap: ImapSession, folder: str, direction: str, uid: int,
                uidvalidity: int, notify: bool) -> bool:
    if MailMessage.objects.filter(account=account, folder=folder, uidvalidity=uidvalidity, uid=uid).exists():
        return False
    raw, flags = imap.fetch(uid)
    if not raw:
        return False
    parsed = parse_message(raw)
    if direction == OUT:
        # Письмо, отправленное через бота, уже сохранено (folder=SENT, без UID) — просто привязываем UID
        own = MailMessage.objects.filter(account=account, direction=OUT, message_id=parsed.message_id)
        if own.exists():
            own.filter(uid__isnull=True).update(folder=folder, uid=uid, uidvalidity=uidvalidity)
            return False
    store_message(account, parsed, raw, direction=direction, folder=folder, uid=uid, uidvalidity=uidvalidity,
                  is_read=direction == OUT or "\\Seen" in flags, notify=notify and direction == IN,
                  store_raw=settings.MAIL_STORE_RAW)
    return True


def _sync_folder(account: MailAccount, imap: ImapSession, folder: str, direction: str,
                 validity_attr: str, last_uid_attr: str) -> int:
    """Новые письма папки. При первом проходе — только история без уведомлений."""
    uidvalidity = imap.select(folder)
    if getattr(account, validity_attr) != uidvalidity:
        first_sync = getattr(account, validity_attr) is None
        uids = imap.all_uids()
        if not first_sync:
            logger.warning("%s/%s: UIDVALIDITY changed -> %s, resync", account, folder, uidvalidity)
        if first_sync:
            # Сначала история, и только потом фиксируем позицию: при обрыве связи
            # следующий проход продолжит импорт (уже загруженные UID пропускаются)
            for uid in uids[-settings.MAIL_INITIAL_IMPORT:]:
                try:
                    _import_uid(account, imap, folder, direction, uid, uidvalidity, notify=False)
                except (imaplib.IMAP4.abort, OSError):
                    raise
                except Exception:
                    logger.exception("%s/%s: initial import uid=%s failed", account, folder, uid)
        setattr(account, validity_attr, uidvalidity)
        setattr(account, last_uid_attr, uids[-1] if uids else 0)
        account.save(update_fields=[validity_attr, last_uid_attr])
        return 0

    new_count = 0
    for uid in imap.uids_after(getattr(account, last_uid_attr))[:settings.MAIL_FETCH_BATCH]:
        try:
            if _import_uid(account, imap, folder, direction, uid, uidvalidity, notify=True):
                new_count += 1
        except (imaplib.IMAP4.abort, OSError):
            raise  # обрыв связи — повторим этот UID в следующем цикле
        except Exception:
            # Битое письмо не должно блокировать очередь
            logger.exception("%s/%s: import uid=%s failed", account, folder, uid)
        setattr(account, last_uid_attr, uid)
        account.save(update_fields=[last_uid_attr])
    return new_count


READ_CHANGE_GRACE = timedelta(minutes=2)


def _sync_read_flags(account: MailAccount, imap: ImapSession) -> list[int]:
    """Статус «прочитано» с сервера -> в БД (письмо открыли в Outlook / веб-почте). Возвращает id изменённых."""
    if not settings.MAIL_READ_SYNC_DAYS or not account.inbox_uidvalidity:
        return []
    now = timezone.now()
    local = dict(
        MailMessage.objects.filter(account=account, folder=INBOX, uidvalidity=account.inbox_uidvalidity,
                                   uid__isnull=False, date__gte=now - timedelta(days=settings.MAIL_READ_SYNC_DAYS))
        # Только что изменено в боте и, возможно, ещё не дошло до сервера — не трогаем
        .exclude(read_changed_at__gte=now - READ_CHANGE_GRACE)
        .values_list("uid", "is_read")
    )
    if not local:
        return []
    flags = imap.flags_since(min(local))
    to_read, to_unread = [], []
    for uid, is_read in local.items():
        if uid not in flags:
            continue  # удалено или перемещено на сервере
        seen = "\\Seen" in flags[uid]
        if seen != is_read:
            (to_read if seen else to_unread).append(uid)
    changed = []
    for uids, value in ((to_read, True), (to_unread, False)):
        if uids:
            qs = MailMessage.objects.filter(account=account, folder=INBOX, uidvalidity=account.inbox_uidvalidity,
                                            uid__in=uids)
            changed += list(qs.values_list("id", flat=True))
            qs.update(is_read=value)
    if changed:
        logger.info("%s: read status updated from server for %s message(s)", account, len(changed))
    return changed


def sync_account(account: MailAccount) -> int:
    """Забирает новые письма из INBOX и «Отправленных». Возвращает количество новых входящих."""
    with ImapSession.for_account(account) as imap:
        new_count = _sync_folder(account, imap, INBOX, IN, "inbox_uidvalidity", "last_uid")
        try:
            read_changed = _sync_read_flags(account, imap)
        except (imaplib.IMAP4.abort, OSError):
            raise
        except Exception as e:
            read_changed = []
            logger.warning("%s: read flags sync failed: %s", account, e)

        if not account.sent_folder:
            try:
                account.sent_folder = imap.find_sent_folder() or ""
            except Exception:
                logger.warning("%s: can't detect Sent folder", account)
        if account.sent_folder:
            try:
                sent = _sync_folder(account, imap, account.sent_folder, OUT, "sent_uidvalidity", "sent_last_uid")
                if sent:
                    logger.info("%s: %s sent message(s) imported", account, sent)
            except (imaplib.IMAP4.abort, OSError):
                raise
            except Exception as e:
                # Проблемы с «Отправленными» не должны мешать входящим
                logger.warning("%s: Sent folder sync failed: %s", account, e)

    account.last_sync_at = timezone.now()
    account.last_error = ""
    account.error_count = 0
    account.save(update_fields=["last_sync_at", "last_error", "error_count", "sent_folder"])
    if new_count:
        events.notify_bot("new_mail", account_id=account.pk, count=new_count)
    if read_changed:
        events.notify_bot("read_changed", message_ids=read_changed)
    return new_count


def mark_needs_reauth(account: MailAccount, error: str) -> None:
    """Пароль больше не подходит: останавливаем синхронизацию и просим пользователя сменить пароль."""
    MailAccount.objects.filter(pk=account.pk).update(
        is_active=False, needs_reauth=True, reauth_notified_at=None,
        last_error=f"Ошибка авторизации: {error}"[:1000], error_count=account.error_count + 1)
    events.notify_bot("account_error", account_id=account.pk)


def run_sync(account_id: int) -> None:
    """Обёртка для воркера: синхронизация + учёт ошибок."""
    account = MailAccount.objects.select_related("owner").get(pk=account_id)
    try:
        count = sync_account(account)
        if count:
            logger.info("%s: %s new message(s)", account, count)
    except ImapAuthError as e:
        # Не долбим сервер неверным паролем — иначе AD заблокирует учётную запись
        logger.warning("%s: auth failed: %s", account, e)
        mark_needs_reauth(account, str(e))
    except Exception as e:
        logger.warning("%s: sync failed: %s", account, e)
        MailAccount.objects.filter(pk=account.pk).update(
            last_error=str(e)[:1000], error_count=account.error_count + 1, last_sync_at=timezone.now())
