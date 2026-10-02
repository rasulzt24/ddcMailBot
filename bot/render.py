"""Текстовое оформление писем для Telegram (HTML parse mode). Вызывается в sync-контексте."""
import html

from django.utils import timezone

from apps.mail.models import MailMessage

TG_LIMIT = 4096

KIND_LABELS = {
    MailMessage.Kind.NEW: "🆕 Новое",
    MailMessage.Kind.REPLY: "↩️ Ответ",
    MailMessage.Kind.FORWARD: "↪️ Переслано",
    MailMessage.Kind.AUTO_REPLY: "🤖 Автоответ",
    MailMessage.Kind.CALENDAR: "📅 Приглашение на встречу",
}


def esc(value) -> str:
    return html.escape(str(value or ""), quote=False)


def fmt_date(dt, short: bool = False) -> str:
    if not dt:
        return "—"
    local = timezone.localtime(dt)
    if short:
        return local.strftime("%d.%m %H:%M")
    return local.strftime("%d.%m.%Y %H:%M")


def fmt_size(size: int) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if size < 1024 or unit == "ГБ":
            return f"{size:.0f} {unit}" if unit == "Б" else f"{size:.1f} {unit}"
        size /= 1024
    return ""


def person(name: str, email: str, own_email: str = "") -> str:
    if own_email and email.lower() == own_email.lower():
        return "<b>Вы</b>"
    if name and email:
        return f"{esc(name)} &lt;{esc(email)}&gt;"
    return esc(name or email)


def short_person(name: str, email: str, own_email: str = "") -> str:
    if own_email and email.lower() == own_email.lower():
        return "Вы"
    return name or email.split("@")[0]


def fmt_people(recipients, own_email: str, limit: int = 6) -> str:
    items = [person(r.name, r.email, own_email) for r in recipients]
    if len(items) > limit:
        return ", ".join(items[:limit]) + f" и ещё {len(items) - limit}"
    return ", ".join(items)


def trim(text: str, limit: int) -> tuple[str, bool]:
    text = (text or "").strip()
    if len(text) <= limit:
        return text, False
    cut = text[:limit]
    space = cut.rfind(" ")
    return (cut[:space] if space > limit * 0.8 else cut) + "…", True


def message_card(m: MailMessage, title: str, thread_pos: int, thread_total: int, body_limit: int = 2800) -> str:
    own = m.account.email
    recipients = list(m.recipients.all())
    to = [r for r in recipients if r.kind == "to"]
    cc = [r for r in recipients if r.kind == "cc"]
    files = [a for a in m.attachments.all() if not a.is_inline]
    inline = [a for a in m.attachments.all() if a.is_inline]

    direction = "📤" if m.direction == MailMessage.Direction.OUTGOING else ""
    lines = [f"{direction}{title} · {KIND_LABELS.get(m.kind, '')}", ""]
    lines.append(f"📌 <b>Тема:</b> {esc(m.subject) or '<i>(без темы)</i>'}")
    lines.append(f"👤 <b>От:</b> {person(m.from_name, m.from_email, own)}")
    if to:
        lines.append(f"📨 <b>Кому:</b> {fmt_people(to, own)}")
    if cc:
        lines.append(f"📋 <b>Копия:</b> {fmt_people(cc, own)}")
    lines.append(f"🕒 <b>Дата:</b> {fmt_date(m.date)}")

    own_l = own.lower()
    if m.direction == MailMessage.Direction.INCOMING and own_l in {r.email for r in cc} \
            and own_l not in {r.email for r in to}:
        lines.append("👁 <i>Вы в копии</i>")
    if m.importance == MailMessage.Importance.HIGH:
        lines.append("❗ <b>Высокая важность</b>")
    if files:
        total = sum(a.size for a in files)
        names = ", ".join(esc(a.filename) for a in files[:5]) + (f" и ещё {len(files) - 5}" if len(files) > 5 else "")
        lines.append(f"📎 <b>Вложения ({len(files)}, {fmt_size(total)}):</b> {names}")
    if inline:
        lines.append(f"🖼 Встроенных изображений: {len(inline)}")
    if thread_total > 1:
        lines.append(f"💬 Письмо {thread_pos} из {thread_total} в цепочке")

    body = m.body_new_text or m.body_text
    note = ""
    if m.kind == MailMessage.Kind.FORWARD and not m.body_new_text:
        body = m.body_text
        note = "<i>Переслано без комментария</i>\n"
    header = "\n".join(lines)
    limit = min(body_limit, TG_LIMIT - len(header) - 300)
    body, cut = trim(body, max(limit, 200))
    if body:
        header += f"\n\n{note}<blockquote expandable>{esc(body)}</blockquote>"
    else:
        header += "\n\n<i>(пустое письмо)</i>"
    if cut or (m.body_new_text and m.body_new_text != m.body_text):
        header += "\n<i>Полный текст с историей — «📄 Полностью»</i>"
    return header


def thread_history(messages: list[MailMessage], own_email: str, per_message: int = 450) -> str:
    """messages — в хронологическом порядке; показываем новые сверху."""
    subject = messages[-1].subject if messages else ""
    head = f"📜 <b>История переписки</b> · {len(messages)} писем · сначала новые\n📌 {esc(subject)}\n"
    blocks = []
    for i, m in reversed(list(enumerate(messages, 1))):
        arrow = "📤" if m.direction == MailMessage.Direction.OUTGOING else "📥"
        text, _ = trim(m.body_new_text or m.body_text, per_message)
        attach = " 📎" if getattr(m, "has_files", False) else ""
        blocks.append(
            f"<b>{i}.</b> {arrow} {fmt_date(m.date, short=True)} · <b>{esc(short_person(m.from_name, m.from_email, own_email))}</b>"
            f" · {KIND_LABELS.get(m.kind, '')}{attach}\n<blockquote expandable>{esc(text) or '—'}</blockquote>"
        )
    # Укладываемся в лимит Telegram — отбрасываем самые старые (они в конце)
    while blocks and len(head) + sum(len(b) + 1 for b in blocks) > TG_LIMIT - 100:
        blocks.pop()
    skipped = len(messages) - len(blocks)
    text = head + "\n" + "\n".join(blocks)
    if skipped:
        text += f"\n<i>…ещё {skipped} ранних писем скрыто</i>"
    return text
