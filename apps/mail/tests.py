from email.message import EmailMessage

from django.test import SimpleTestCase

from .services.parser import normalize_subject, parse_message, split_quoted


def _outlook_reply() -> bytes:
    msg = EmailMessage()
    msg["From"] = "=?utf-8?B?0JjQstCw0L3QvtCyINCY0LLQsNC9?= <ivanov@example.kz>"
    msg["To"] = "Me <me@example.kz>, petrov@example.kz"
    msg["Cc"] = '"Сидоров, Пётр" <sidorov@example.kz>'
    msg["Subject"] = "RE: Отчёт за сентябрь"
    msg["Message-ID"] = "<reply-1@example.kz>"
    msg["In-Reply-To"] = "<orig-1@example.kz>"
    msg["References"] = "<orig-1@example.kz>"
    msg["Importance"] = "High"
    msg.set_content("Добрый день!\nСогласовано.\n\nОт: Me\nОтправлено: 1 сентября\nКому: Иванов\nТема: Отчёт\n\nстарый текст")
    msg.add_alternative('<html><body><p>Добрый день!</p><img src="cid:image001.png@01DB"></body></html>',
                        subtype="html")
    html_part = msg.get_body(preferencelist=("html",))
    html_part.add_related(b"\x89PNG....", maintype="image", subtype="png", cid="<image001.png@01DB>",
                          filename="image001.png")
    msg.add_attachment(b"%PDF-1.4 test", maintype="application", subtype="pdf", filename="Отчёт.pdf")
    return msg.as_bytes()


def _forward_with_eml() -> bytes:
    inner = EmailMessage()
    inner["From"] = "boss@example.kz"
    inner["Subject"] = "Приказ"
    inner.set_content("Текст приказа")
    msg = EmailMessage()
    msg["From"] = "colleague@example.kz"
    msg["To"] = "me@example.kz"
    msg["Subject"] = "FW: Приказ"
    msg.set_content("Смотри ниже")
    msg.add_attachment(inner)
    return msg.as_bytes()


class ParserTests(SimpleTestCase):
    def test_reply_headers_and_attachments(self):
        p = parse_message(_outlook_reply())
        self.assertEqual(p.from_.name, "Иванов Иван")
        self.assertEqual(p.from_.email, "ivanov@example.kz")
        self.assertEqual([a.email for a in p.to], ["me@example.kz", "petrov@example.kz"])
        self.assertEqual(p.cc[0].name, "Сидоров, Пётр")
        self.assertEqual(p.kind, "reply")
        self.assertEqual(p.importance, "high")
        self.assertEqual(p.in_reply_to, "<orig-1@example.kz>")
        files = {a.filename: a for a in p.attachments}
        self.assertTrue(files["image001.png"].is_inline)
        self.assertFalse(files["Отчёт.pdf"].is_inline)
        self.assertEqual(p.new_text, "Добрый день!\nСогласовано.")

    def test_forward_with_attached_message(self):
        p = parse_message(_forward_with_eml())
        self.assertEqual(p.kind, "forward")
        self.assertEqual(p.text, "Смотри ниже")  # вложенное письмо не смешивается с телом
        self.assertTrue(any(a.content_type == "message/rfc822" for a in p.attachments))

    def test_normalize_subject(self):
        self.assertEqual(normalize_subject("RE: FW: Ответ: Отчёт  за  месяц"), "отчёт за месяц")
        self.assertEqual(normalize_subject("RE[2]: test"), "test")

    def test_split_quoted_variants(self):
        self.assertEqual(split_quoted("Ок\n\n-----Original Message-----\nFrom: x"), "Ок")
        self.assertEqual(split_quoted("Ок\n________________________________\nОт: x"), "Ок")
        self.assertEqual(split_quoted("Ок\nOn Mon, 1 Sep 2026 Ivan wrote:\n> old"), "Ок")
        self.assertEqual(split_quoted("Без цитаты"), "Без цитаты")
