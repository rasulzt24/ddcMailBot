"""Наполняет тестовый почтовый сервер (GreenMail из docker-compose.test.yml) письмами разных видов.

    $env:ENV_FILE=".env.test"; ..\\.venv\\Scripts\\python manage.py mail_seed_test
    ... mail_seed_test --extra 40      # + 40 простых писем (для проверки страниц списка)

Работает только с локальным почтовым сервером — рабочую почту не трогает.
"""
import imaplib
import io
import smtplib
import ssl
import struct
import time
import zipfile
import zlib
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

DOMAIN = "test.local"
PASSWORD = "test123"
ME = f"me@{DOMAIN}"
COLLEAGUE = f"colleague@{DOMAIN}"
BOSS = f"boss@{DOMAIN}"
NAMES = {ME: "Тестов Тест", COLLEAGUE: "Коллегова Анна", BOSS: "Начальников Иван Петрович"}
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "mail"}


def _ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _png(color=(30, 120, 220), size=24) -> bytes:
    """Маленькая валидная PNG-картинка без сторонних библиотек."""
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    row = b"\x00" + bytes(color) * size
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(row * size)) + chunk(b"IEND", b""))


def _pdf(text="Test PDF") -> bytes:
    body = f"BT /F1 24 Tf 72 720 Td ({text}) Tj ET".encode()
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
            b"/Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length %d >>\nstream\n" % len(body) + body + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offsets = io.BytesIO(), []
    out.write(b"%PDF-1.4\n")
    for i, obj in enumerate(objs, 1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % i + obj + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1))
    for off in offsets:
        out.write(b"%010d 00000 n \n" % off)
    out.write(b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref))
    return out.getvalue()


def _zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("readme.txt", "Тестовый архив\n")
        z.writestr("data/table.csv", "id;name\n1;Первый\n2;Второй\n")
    return buf.getvalue()


def _addr(email: str) -> str:
    return f'"{NAMES[email]}" <{email}>'


class Command(BaseCommand):
    help = "Заполнить тестовые ящики GreenMail письмами разных типов"

    def add_arguments(self, parser):
        parser.add_argument("--extra", type=int, default=0, help="дополнительно N простых писем")

    def handle(self, *args, extra, **options):
        for host in (settings.MAIL_IMAP_HOST, settings.MAIL_SMTP_HOST):
            if host not in LOCAL_HOSTS:
                raise CommandError(f"Почтовый сервер «{host}» не локальный. Команда работает только с тестовой "
                                   f"средой: запустите с ENV_FILE=.env.test")
        self.now = datetime.now().astimezone()
        self._create_bucket()
        for user in (ME, COLLEAGUE, BOSS):
            self._ensure_sent_folder(user)

        n = self._seed()
        for i in range(1, extra + 1):
            self._send(COLLEAGUE, [ME], f"Тестовое письмо #{i}", f"Простое письмо номер {i} для проверки списка.",
                       hours_ago=200 + i)
            n += 1
        self.stdout.write(self.style.SUCCESS(f"Готово: отправлено писем — {n}. Ящик для бота: {ME} / {PASSWORD}"))

    # ---------- инфраструктура ----------

    def _create_bucket(self):
        if not settings.S3_ENDPOINT_URL:
            return
        from django.core.files.storage import default_storage
        client = default_storage.connection.meta.client
        bucket = default_storage.bucket_name
        try:
            client.head_bucket(Bucket=bucket)
        except Exception:
            client.create_bucket(Bucket=bucket)
            self.stdout.write(f"S3: создан бакет {bucket}")

    def _imap(self, user: str) -> imaplib.IMAP4_SSL:
        imap = imaplib.IMAP4_SSL(settings.MAIL_IMAP_HOST, settings.MAIL_IMAP_PORT, ssl_context=_ctx(), timeout=30)
        imap.login(user, PASSWORD)
        return imap

    def _ensure_sent_folder(self, user: str):
        imap = self._imap(user)
        try:
            _, folders = imap.list()
            if not any(b'"Sent"' in f or f.endswith(b" Sent") for f in folders or []):
                imap.create("Sent")
        finally:
            imap.logout()

    # ---------- отправка ----------

    def _message(self, sender, to, subject, text, *, cc=(), hours_ago=0, html=None, headers=None,
                 message_id=None) -> EmailMessage:
        msg = EmailMessage()
        msg["From"] = _addr(sender)
        msg["To"] = ", ".join(_addr(a) if a in NAMES else a for a in to)
        if cc:
            msg["Cc"] = ", ".join(_addr(a) if a in NAMES else a for a in cc)
        msg["Subject"] = subject
        msg["Date"] = format_datetime(self.now - timedelta(hours=hours_ago))
        msg["Message-ID"] = message_id or make_msgid(domain=DOMAIN)
        for k, v in (headers or {}).items():
            msg[k] = v
        msg.set_content(text)
        if html:
            msg.add_alternative(html, subtype="html")
        return msg

    def _deliver(self, sender: str, msg: EmailMessage, recipients: list[str], save_to_sent: bool = True):
        with smtplib.SMTP_SSL(settings.MAIL_SMTP_HOST, settings.MAIL_SMTP_PORT, context=_ctx(), timeout=30) as smtp:
            smtp.login(sender, PASSWORD)
            smtp.send_message(msg, from_addr=sender, to_addrs=recipients)
        if save_to_sent:
            imap = self._imap(sender)
            try:
                imap.append("Sent", r"(\Seen)", imaplib.Time2Internaldate(time.time()), msg.as_bytes())
            finally:
                imap.logout()

    def _send(self, sender, to, subject, text, *, cc=(), **kw) -> EmailMessage:
        msg = self._message(sender, to, subject, text, cc=cc, **kw)
        self._deliver(sender, msg, list(to) + list(cc))
        return msg

    # ---------- сценарии ----------

    def _seed(self) -> int:
        count = 0

        # 1. Новое письмо, высокая важность, вы в копии
        self._send(BOSS, [COLLEAGUE], "Совещание в пятницу в 10:00",
                   "Коллеги, добрый день!\n\nВ пятницу в 10:00 совещание по итогам квартала, переговорная 305.\n"
                   "Прошу подготовить короткие отчёты.\n\nС уважением,\nИ. П. Начальников",
                   cc=[ME], hours_ago=50, headers={"Importance": "High", "X-Priority": "1"})
        count += 1

        # 2. Вложения разных типов: PDF, ZIP, PNG, TXT
        msg = self._message(COLLEAGUE, [ME], "Отчёт за сентябрь (вложения)",
                            "Привет!\n\nВо вложении отчёт, архив с данными и скриншот.\n\nАнна", hours_ago=40)
        msg.add_attachment(_pdf("September report"), maintype="application", subtype="pdf", filename="Отчёт_сентябрь.pdf")
        msg.add_attachment(_zip(), maintype="application", subtype="zip", filename="данные.zip")
        msg.add_attachment(_png((220, 60, 60), 64), maintype="image", subtype="png", filename="скриншот.png")
        msg.add_attachment("Строка 1\nСтрока 2\n".encode(), maintype="text", subtype="plain", filename="заметки.txt")
        self._deliver(COLLEAGUE, msg, [ME])
        count += 1

        # 3. HTML с встроенной картинкой подписи (cid:), как у Outlook
        msg = self._message(BOSS, [ME], "Пример HTML-письма с подписью",
                            "Добрый день!\n\nЭто HTML-письмо с картинкой в подписи.\n\n--\nИ. П. Начальников",
                            hours_ago=30)
        msg.add_alternative('<html><body><p>Добрый день!</p><p>Это <b>HTML-письмо</b> с картинкой в подписи.</p>'
                            '<p>--<br>И. П. Начальников<br><img src="cid:logo001@test.local"></p></body></html>',
                            subtype="html")
        msg.get_body(preferencelist=("html",)).add_related(_png((40, 160, 90), 32), maintype="image", subtype="png",
                                                          cid="<logo001@test.local>", filename="image001.png")
        self._deliver(BOSS, msg, [ME])
        count += 1

        # 4. Цепочка: письмо -> ваш ответ (в «Отправленных») -> ответ на ответ
        first = self._send(BOSS, [ME], "Бюджет на 2027 год",
                           "Подготовьте, пожалуйста, предложения по бюджету до среды.", hours_ago=26)
        reply_text = "Иван Петрович, добрый день!\n\nПредложения подготовлю ко вторнику.\n\nТест"
        reply = self._send(ME, [BOSS], "RE: Бюджет на 2027 год",
                           f"{reply_text}\n\n-----Original Message-----\nFrom: {NAMES[BOSS]}\n"
                           f"Subject: Бюджет на 2027 год\n\nПодготовьте, пожалуйста, предложения по бюджету до среды.",
                           hours_ago=25, headers={"In-Reply-To": first["Message-ID"],
                                                  "References": first["Message-ID"]})
        self._send(BOSS, [ME], "RE: RE: Бюджет на 2027 год",
                   f"Отлично, жду.\n\n-----Original Message-----\nFrom: {NAMES[ME]}\nSubject: RE: Бюджет на 2027 год\n\n"
                   f"{reply_text}", cc=[COLLEAGUE], hours_ago=24,
                   headers={"In-Reply-To": reply["Message-ID"],
                            "References": f"{first['Message-ID']} {reply['Message-ID']}"})
        count += 3

        # 5. Пересылка с вложенным письмом (.eml)
        inner = self._message(BOSS, [COLLEAGUE], "Приказ №15 о графике отпусков",
                              "Утвердить график отпусков на 2027 год согласно приложению.", hours_ago=60)
        msg = self._message(COLLEAGUE, [ME], "FW: Приказ №15 о графике отпусков",
                            "Пересылаю для ознакомления.\n\n-------- Пересылаемое сообщение --------\n"
                            f"От: {NAMES[BOSS]}\nТема: Приказ №15 о графике отпусков\n\n"
                            "Утвердить график отпусков на 2027 год согласно приложению.", hours_ago=20)
        msg.add_attachment(inner)
        self._deliver(COLLEAGUE, msg, [ME])
        count += 1

        # 6. Автоответ
        self._send(BOSS, [ME], "Автоматический ответ: Бюджет на 2027 год",
                   "Я в отпуске до 15 октября. По срочным вопросам — Коллегова Анна.", hours_ago=12,
                   headers={"Auto-Submitted": "auto-replied"})
        count += 1

        # 7. Приглашение на встречу (text/calendar)
        msg = self._message(BOSS, [ME, COLLEAGUE], "Приглашение: Планёрка", "Приглашаю на планёрку.", hours_ago=8)
        start = (self.now + timedelta(days=1)).strftime("%Y%m%dT100000")
        msg.add_attachment(
            "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nMETHOD:REQUEST\r\nBEGIN:VEVENT\r\n"
            f"UID:{make_msgid(domain=DOMAIN)}\r\nDTSTART:{start}\r\nSUMMARY:Планёрка\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n",
            subtype="calendar", filename="invite.ics")
        self._deliver(BOSS, msg, [ME, COLLEAGUE])
        count += 1

        # 8. Длинное письмо — проверка сокращения и «Полностью»
        paragraph = ("Это длинный абзац для проверки того, как бот сокращает большие письма и показывает "
                     "кнопку «Полностью». ") * 6
        self._send(COLLEAGUE, [ME], "Длинное письмо", "\n\n".join(f"{i}. {paragraph}" for i in range(1, 16)),
                   hours_ago=4)
        count += 1

        # 9. Свежее письмо от коллеги на английском — проверка «новые сверху»
        self._send(COLLEAGUE, [ME], "Quick question", "Hi! Do you have a minute today?\n\nAnna", hours_ago=1)
        count += 1
        return count
