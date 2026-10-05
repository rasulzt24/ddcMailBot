from aiogram.types import InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

BTN_INBOX = "📥 Входящие"
BTN_SENT = "📤 Отправленные"
BTN_COMPOSE = "✉️ Написать"
BTN_SEARCH = "🔎 Поиск"
BTN_MAILBOX = "📬 Моя почта"
BTN_SETTINGS = "⚙️ Настройки"
BTN_ADMIN = "🛡 Админ"
BTN_WORKDAY = "⏱ Рабочий день"
MENU_BUTTONS = {BTN_INBOX, BTN_SENT, BTN_COMPOSE, BTN_SEARCH, BTN_MAILBOX, BTN_SETTINGS, BTN_ADMIN, BTN_WORKDAY}


def main_menu(is_superadmin: bool = False) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text=BTN_INBOX), KeyboardButton(text=BTN_COMPOSE)],
        [KeyboardButton(text=BTN_SENT), KeyboardButton(text=BTN_SEARCH)],
        [KeyboardButton(text=BTN_MAILBOX), KeyboardButton(text=BTN_SETTINGS)],
    ]
    from django.conf import settings
    if settings.BITRIX_ENABLED:
        rows.insert(0, [KeyboardButton(text=BTN_WORKDAY)])
    if is_superadmin:
        rows.append([KeyboardButton(text=BTN_ADMIN)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=True,
                               input_field_placeholder="Выберите действие")


def message_actions(message_id: int, *, has_files: bool, has_inline: bool, can_reply_all: bool,
                    in_thread: bool, is_read: bool, incoming: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="↩️ Ответить", callback_data=f"m:r:{message_id}")
    if can_reply_all:
        kb.button(text="↩️ Всем", callback_data=f"m:ra:{message_id}")
    kb.button(text="↪️ Переслать", callback_data=f"m:f:{message_id}")
    sizes = [3 if can_reply_all else 2]

    second = 0
    if in_thread:
        kb.button(text="📜 История", callback_data=f"m:h:{message_id}")
        second += 1
    kb.button(text="📄 Полностью", callback_data=f"m:full:{message_id}")
    second += 1
    if has_files:
        kb.button(text="📎 Вложения", callback_data=f"m:att:{message_id}")
        second += 1
    sizes.append(second)

    third = 0
    if has_inline:
        kb.button(text="🖼 Картинки", callback_data=f"m:img:{message_id}")
        third += 1
    if incoming:
        kb.button(text="✉️ Непрочитано" if is_read else "✅ Прочитано", callback_data=f"m:seen:{message_id}")
        third += 1
    if third:
        sizes.append(third)
    kb.adjust(*sizes)
    return kb.as_markup()


def full_view_actions(message_id: int, has_html: bool, has_raw: bool) -> InlineKeyboardMarkup | None:
    kb = InlineKeyboardBuilder()
    if has_html:
        kb.button(text="🌐 HTML-версия", callback_data=f"m:html:{message_id}")
    if has_raw:
        kb.button(text="📦 Оригинал .eml", callback_data=f"m:eml:{message_id}")
    kb.adjust(2)
    return kb.as_markup() if (has_html or has_raw) else None


def cancel_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data="d:cancel")
    return kb.as_markup()


SEARCH_BUTTON = "🔎 Найти в справочнике"


def recipients_kb(field: str, contacts, has_items: bool, optional: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    # Inline-режим: в поле ввода подставится «@бот », дальше подсказки появляются по мере набора
    kb.button(text=SEARCH_BUTTON, switch_inline_query_current_chat="")
    for c in contacts:
        label = c.name or c.email
        kb.button(text=f"➕ {label[:30]}", callback_data=f"cp:{field}:{c.pk}")
    sizes = [1] + [2] * ((len(contacts) + 1) // 2)
    row = 0
    if has_items:
        kb.button(text="🧹 Очистить", callback_data=f"cclear:{field}")
        row += 1
    if has_items:
        kb.button(text="➡️ Далее", callback_data=f"cn:{field}")
        row += 1
    elif optional:
        kb.button(text="⏭ Без копии", callback_data=f"cn:{field}")
        row += 1
    if row:
        sizes.append(row)
    kb.button(text="❌ Отмена", callback_data="d:cancel")
    sizes.append(1)
    kb.adjust(*sizes)
    return kb.as_markup()


def pick_contacts_kb(field: str, contacts) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for c in contacts:
        kb.button(text=f"{c.name or ''} <{c.email}>"[:60], callback_data=f"cp:{field}:{c.pk}")
    kb.adjust(1)
    return kb.as_markup()


def skip_kb(callback: str, text: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=text, callback_data=callback)
    kb.button(text="❌ Отмена", callback_data="d:cancel")
    kb.adjust(1)
    return kb.as_markup()


def draft_kb(mode: str, include_attachments: bool, source_files: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🚀 Отправить", callback_data="d:send")
    kb.button(text="✏️ Кому", callback_data="d:edit:to")
    kb.button(text="✏️ Копия", callback_data="d:edit:cc")
    kb.button(text="✏️ Тема", callback_data="d:edit:subject")
    kb.button(text="✏️ Текст", callback_data="d:edit:body")
    kb.button(text="📎 Файлы", callback_data="d:edit:files")
    sizes = [1, 2, 3]
    if source_files:
        state = "вкл" if include_attachments else "выкл"
        kb.button(text=f"📎 Вложения оригинала ({source_files}): {state}", callback_data="d:toggle_att")
        sizes.append(1)
    kb.button(text="🗑 Отменить", callback_data="d:cancel")
    sizes.append(1)
    kb.adjust(*sizes)
    return kb.as_markup()


def pager(prefix: str, page: int, has_next: bool) -> list:
    kb = InlineKeyboardBuilder()
    if page > 0:
        kb.button(text="◀️", callback_data=f"{prefix}:{page - 1}")
    if has_next:
        kb.button(text="▶️", callback_data=f"{prefix}:{page + 1}")
    return list(kb.buttons)


def reauth_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🔑 Ввести новый пароль", callback_data="acc:repass")
    return kb.as_markup()


REAUTH_BANNER = ("🔐 <b>Пароль от почты не подходит</b> — проверка почты и отправка остановлены.\n"
                 "Нажмите «🔑 Ввести новый пароль».")


def people_pick_kb(field: str, people, offset: int) -> InlineKeyboardMarkup:
    """Выбор из найденных в справочнике; индексы ссылаются на список в FSM (адреса в callback не влезают)."""
    kb = InlineKeyboardBuilder()
    for i, p in enumerate(people):
        details = f" — {p.details}" if p.details else ""
        kb.button(text=f"{p.name or p.email}{details}"[:64], callback_data=f"px:{field}:{offset + i}")
    kb.button(text=SEARCH_BUTTON, switch_inline_query_current_chat="")
    kb.adjust(1)
    return kb.as_markup()
