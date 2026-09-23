"""Inline keyboards for the single-message navigation panel (see panel.py).
No ReplyKeyboardMarkup here on purpose — the bottom-of-screen button row
was the exact clutter the panel design replaces.
"""
from __future__ import annotations

from maxapi.types import InlineKeyboardButton, InlineKeyboardMarkup

Row = list[InlineKeyboardButton]


def main_menu_kb(is_staff: bool) -> InlineKeyboardMarkup:
    rows: list[Row] = [
        [InlineKeyboardButton(text="📅 Сегодня", callback_data="menu:today"),
         InlineKeyboardButton(text="📝 Пропуск", callback_data="menu:absence")],
    ]
    if is_staff:
        rows.append([
            InlineKeyboardButton(text="🗓 Расписание", callback_data="menu:schedule"),
            InlineKeyboardButton(text="✏️ Корректировка", callback_data="menu:override"),
        ])
        rows.append([
            InlineKeyboardButton(text="📊 Отчёт", callback_data="menu:report"),
            InlineKeyboardButton(text="🔗 Ссылки", callback_data="menu:invites"),
        ])
        rows.append([InlineKeyboardButton(text="👥 Кто на паре", callback_data="menu:live")])
        rows.append([
            InlineKeyboardButton(text="🗂 Карточка студента", callback_data="menu:card"),
            InlineKeyboardButton(text="📝➕ Групповой пропуск", callback_data="menu:bulk_excuse"),
        ])
    rows.append([InlineKeyboardButton(text="❓ Помощь", callback_data="menu:help")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def back_to_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅ В меню", callback_data="menu:home")]])


def with_back_to_menu(rows: list[Row]) -> InlineKeyboardMarkup:
    """Appends a trailing '⬅ В меню' row to an existing set of button rows."""
    return InlineKeyboardMarkup(inline_keyboard=[*rows, [InlineKeyboardButton(text="⬅ В меню", callback_data="menu:home")]])
