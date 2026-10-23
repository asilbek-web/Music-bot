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
        [_button("🔍 Search music", "menu:search")],
        [_button("🕘 History", "menu:history"), _button("ℹ️ Help", "menu:help")],
    )


def back_to_menu() -> InlineKeyboardMarkup:
    return _rows([_button("⬅️ Back to menu", "menu:home")])


def search_prompt() -> InlineKeyboardMarkup:
    return _rows([_button("⬅️ Back", "menu:home")])


def search_results(
    tracks: Sequence[Mapping[str, Any]],
    page: int,
    page_size: int,
    search_id: str,
) -> InlineKeyboardMarkup:
    """Numbered result buttons plus pagination."""
    total_pages = max(1, ceil(len(tracks) / page_size))
    page = max(0, min(page, total_pages - 1))
    start = page * page_size
    visible = tracks[start : start + page_size]

    rows: list[list[InlineKeyboardButton]] = []
    for offset, track in enumerate(visible):
        index = start + offset
        label = f"{index + 1}. {track.get('title', '?')} — {track.get('artist', '?')}"
        rows.append([_button(_short(label), f"t:{search_id}:{index}")])

    navigation: list[InlineKeyboardButton] = []
    if page > 0:
        navigation.append(_button("⬅️", f"s:{search_id}:{page - 1}"))
    if page < total_pages - 1:
        navigation.append(_button("➡️", f"s:{search_id}:{page + 1}"))
    if navigation:
        rows.append(navigation)

    rows.append([_button("🔍 New search", "menu:search"), _button("✖️ Close", "close")])
    return _rows(*rows)


def track_actions(search_id: str, index: int, page: int) -> InlineKeyboardMarkup:
    return _rows(
        [_button("⬇️ Get audio", f"d:{search_id}:{index}")],
        [
            _button("⬅️ Back to results", f"s:{search_id}:{page}"),
            _button("✖️ Close", "close"),
        ],
    )


def history_list(entries: Sequence[Mapping[str, Any]]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for position, entry in enumerate(entries, start=1):
        label = f"▶️ {position}. {entry['title']} — {entry['artist']}"
        rows.append([_button(_short(label), f"h:{entry['id']}")])
    rows.append([_button("⬅️ Back to menu", "menu:home")])
    return _rows(*rows)


def admin_panel() -> InlineKeyboardMarkup:
    return _rows(
        [_button("🔄 Refresh", "admin:refresh")],
        [_button("📢 Broadcast", "admin:broadcast"), _button("✖️ Close", "close")],
    )


def broadcast_confirm(users: int) -> InlineKeyboardMarkup:
    return _rows(
        [_button(f"✅ Send to {users} user(s)", "bc:start")],
        [_button("❌ Cancel", "bc:cancel")],
    )


def broadcast_prompt() -> InlineKeyboardMarkup:
    return _rows([_button("❌ Cancel", "bc:cancel")])


def cancel_only() -> InlineKeyboardMarkup:
    return _rows([_button("✖️ Cancel", "bc:cancel")])
