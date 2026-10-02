"""Сохраняет сертификаты IMAP/SMTP-серверов в PEM-файл доверенных (certificate pinning).

Нужно, когда сервер подписан внутренним CA (NBRK-CA) или самоподписан (Exchange EX-2).
Правильнее — положить в файл корневой сертификат NBRK-CA от ИТ; эта команда — рабочая альтернатива.

    python manage.py mail_trust_certs [--out certs/mail_trusted.pem]
"""
import hashlib
import smtplib
import socket
import ssl
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

TIMEOUT = 15


def _insecure_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def imap_cert(host: str, port: int) -> bytes:
    with socket.create_connection((host, port), timeout=TIMEOUT) as sock:
        with _insecure_ctx().wrap_socket(sock, server_hostname=host) as tls:
            return tls.getpeercert(binary_form=True)


def smtp_cert(host: str, port: int, security: str) -> bytes:
    if security == "ssl":
        return imap_cert(host, port)
    smtp = smtplib.SMTP(host, port, timeout=TIMEOUT)
    try:
        smtp.ehlo()
        smtp.starttls(context=_insecure_ctx())
        return smtp.sock.getpeercert(binary_form=True)
    finally:
        try:
            smtp.quit()
        except Exception:
            pass


class Command(BaseCommand):
    help = "Закрепить (доверять) текущие сертификаты почтовых серверов"

    def add_arguments(self, parser):
        parser.add_argument("--out", default="certs/mail_trusted.pem")

    def handle(self, *args, out, **options):
        path = Path(out)
        if not path.is_absolute():
            path = Path(settings.BASE_DIR) / path
        path.parent.mkdir(parents=True, exist_ok=True)

        # За одним именем бывает несколько серверов (Exchange EX-1/EX-2/EX-3) — у каждого свой сертификат
        targets = [("IMAP", settings.MAIL_IMAP_HOST, settings.MAIL_IMAP_PORT)]
        if settings.MAIL_SMTP_SECURITY != "none":
            targets.append(("SMTP", settings.MAIL_SMTP_HOST, settings.MAIL_SMTP_PORT))

        certs: dict[bytes, list[str]] = {}
        for name, host, port in targets:
            ips = sorted({a[4][0] for a in socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)})
            for ip in ips:
                try:
                    if name == "IMAP":
                        der = imap_cert(ip, port)
                    else:
                        der = smtp_cert(ip, port, settings.MAIL_SMTP_SECURITY)
                except Exception as e:
                    self.stderr.write(f"{name} {ip}:{port}: {e}")
                    continue
                certs.setdefault(der, []).append(f"{name} {host} {ip}:{port}")

        pem = []
        for der, where in certs.items():
            fp = hashlib.sha256(der).hexdigest().upper()
            fingerprint = ":".join(fp[i:i + 2] for i in range(0, 64, 2))
            self.stdout.write(f"{', '.join(where)}\n    SHA-256: {fingerprint}")
            pem.append(f"# {'; '.join(where)}\n{ssl.DER_cert_to_PEM_cert(der)}")
        if not pem:
            raise SystemExit("Не удалось получить ни одного сертификата")
        path.write_text("\n".join(pem), encoding="ascii")
        self.stdout.write(self.style.SUCCESS(f"Сохранено сертификатов: {len(pem)} -> {path}"))
        self.stdout.write("В .env:\nMAIL_SSL_CAFILE=certs/mail_trusted.pem\nMAIL_SMTP_CHECK_HOSTNAME=False")
