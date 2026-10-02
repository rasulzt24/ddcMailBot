import smtplib
from email.message import EmailMessage

from django.conf import settings

from .imap import ssl_context

SMTP_TIMEOUT = 60


def _context():
    return ssl_context(check_hostname=settings.MAIL_SMTP_CHECK_HOSTNAME)


def _connect(host: str, port: int, security: str) -> smtplib.SMTP:
    if security == "ssl":
        return smtplib.SMTP_SSL(host, port, timeout=SMTP_TIMEOUT, context=_context())
    smtp = smtplib.SMTP(host, port, timeout=SMTP_TIMEOUT)
    smtp.ehlo()
    if security == "starttls":
        smtp.starttls(context=_context())
        smtp.ehlo()
    return smtp


def check_login(host: str, port: int, security: str, login: str, password: str) -> None:
    smtp = _connect(host, port, security)
    try:
        smtp.login(login, password)
    finally:
        try:
            smtp.quit()
        except Exception:
            pass


def send(account, msg: EmailMessage, recipients: list[str]) -> None:
    smtp = _connect(account.smtp_host, account.smtp_port, account.smtp_security)
    try:
        smtp.login(account.login, account.password)
        smtp.send_message(msg, from_addr=account.email, to_addrs=recipients)
    finally:
        try:
            smtp.quit()
        except Exception:
            pass
