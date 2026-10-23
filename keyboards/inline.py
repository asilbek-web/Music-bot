"""Inline keyboards for navigation, search results and admin actions."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from math import ceil
from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

MAX_BUTTON_TEXT = 56


def _short(text: str, limit: int = MAX_BUTTON_TEXT) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _rows(*rows: Sequence[InlineKeyboardButton]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[list(row) for row in rows if row])


def _button(text: str, callback_data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=callback_data)


def main_menu() -> InlineKeyboardMarkup:
    return _rows(
        [_button("🔍 Qo‘shiq qidirish", "menu:search")],
        [_button("🕘 Tarixim", "menu:history"), _button("ℹ️ Yordam", "menu:help")],
    )


def back_to_menu() -> InlineKeyboardMarkup:
    return _rows([_button("⬅️ Asosiy menyu", "menu:home")])


def search_prompt() -> InlineKeyboardMarkup:
    return _rows([_button("⬅️ Orqaga", "menu:home")])


def search_results(
    tracks: Sequence[Mapping[str, Any]],
    page: int,
    page_size: int,
    search_id: str,
) -> InlineKeyboardMarkup:
    """Number keys for the tracks on the current page plus pagination."""
    total_pages = max(1, ceil(len(tracks) / page_size))
    page = max(0, min(page, total_pages - 1))
    start = page * page_size
    visible = tracks[start : start + page_size]

    numbers = [
        _button(str(start + offset + 1), f"p:{search_id}:{start + offset}")
        for offset in range(len(visible))
    ]
    rows: list[list[InlineKeyboardButton]] = [numbers[0:5], numbers[5:10]]

    navigation: list[InlineKeyboardButton] = []
    if page > 0:
        navigation.append(_button("⬅️", f"s:{search_id}:{page - 1}"))
    navigation.append(_button("✖️", "close"))
    if page < total_pages - 1:
        navigation.append(_button("➡️", f"s:{search_id}:{page + 1}"))
    rows.append(navigation)

    return _rows(*rows)


def history_list(entries: Sequence[Mapping[str, Any]]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for position, entry in enumerate(entries, start=1):
        label = f"▶️ {position}. {entry['title']} — {entry['artist']}"
        rows.append([_button(_short(label), f"h:{entry['id']}")])
    rows.append([_button("⬅️ Asosiy menyu", "menu:home")])
    return _rows(*rows)


def admin_panel() -> InlineKeyboardMarkup:
    return _rows(
        [_button("🔄 Yangilash", "admin:refresh")],
        [_button("📢 Xabar yuborish", "admin:broadcast"), _button("✖️ Yopish", "close")],
    )


def broadcast_confirm(users: int) -> InlineKeyboardMarkup:
    return _rows(
        [_button(f"✅ {users} foydalanuvchiga yuborish", "bc:start")],
        [_button("❌ Bekor qilish", "bc:cancel")],
    )


def broadcast_prompt() -> InlineKeyboardMarkup:
    return _rows([_button("❌ Bekor qilish", "bc:cancel")])


def cancel_only() -> InlineKeyboardMarkup:
    return _rows([_button("✖️ Bekor qilish", "bc:cancel")])
