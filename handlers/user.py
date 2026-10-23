"""User-facing handlers: menu, search, track cards, downloads and history."""

from __future__ import annotations

import html
import logging
from collections.abc import Awaitable, Callable, Sequence
from secrets import token_hex
from typing import Any

from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.enums import ChatAction
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, FSInputFile, Message, TelegramObject

from config import Settings
from database.db import Database
from keyboards import inline
from services.audio import AudioError, AudioService, safe_filename
from services.search import MusicSearch, SearchError

log = logging.getLogger(__name__)

router = Router(name="user")
PAGE_SIZE = 5

BANNED_TEXT = "🚫 You are banned from using this bot."
SEARCH_PROMPT = "🔎 Send the song title or artist you are looking for."
SEARCH_ERROR_TEXT = "⚠️ The music catalog is unreachable right now. Please try again in a moment."
NO_RESULTS_TEXT = (
    "😕 No results for <b>{query}</b>.\n\n"
    "Try a different spelling, add the artist name, or search for the artist only."
)
SEARCH_EXPIRED_TEXT = "⌛ This search has expired. Send the song name again to start a new one."
OUTDATED_TEXT = "This button is no longer active. Send a new search."
AUDIO_ERROR_TEXT = "⚠️ I couldn't deliver that audio. Please try again, or pick another track."
HISTORY_EMPTY_TEXT = "🕘 You haven't downloaded any tracks yet.\n\nSend me a song name to get started."


class SearchStates(StatesGroup):
    waiting_query = State()


class UserMiddleware(BaseMiddleware):
    """Registers every user, blocks banned ones and injects the database."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None or user.is_bot:
            return await handler(event, data)

        await self._db.upsert_user(user.id, user.username, user.full_name, user.language_code)

        if await self._db.is_banned(user.id):
            if isinstance(event, CallbackQuery):
                await event.answer(BANNED_TEXT, show_alert=True)
            elif isinstance(event, Message):
                await event.answer(BANNED_TEXT)
            return None

        data["db"] = self._db
        return await handler(event, data)


def _start_text(name: str) -> str:
    return (
        f"<b>Hi, {html.escape(name)}!</b>\n\n"
        "Send me a song title or an artist and I'll find the audio for you.\n\n"
        "Use the buttons below, /search, or simply type a song name."
    )


HELP_TEXT = (
    "<b>How it works</b>\n\n"
    "1. Send a song title or artist name, or use /search.\n"
    "2. Pick a track from the results.\n"
    "3. Tap <b>Get audio</b> and the track arrives as an audio file.\n\n"
    "<b>Commands</b>\n"
    "/start – main menu\n"
    "/search – search for music\n"
    "/history – your recent downloads\n"
    "/help – this message\n\n"
    "<i>Audio previews come from the Apple Music catalog and are about 30 seconds long.</i>"
)


def _format_duration(seconds: int) -> str:
    minutes, secs = divmod(max(0, seconds), 60)
    return f"{minutes}:{secs:02d}"


def _results_text(query: str, total: int, page: int, page_size: int) -> str:
    first = page * page_size + 1
    last = min(total, (page + 1) * page_size)
    total_pages = max(1, (total + page_size - 1) // page_size)
    return (
        f"🔎 Results for <b>{html.escape(query)}</b>\n"
        f"Showing {first}–{last} of {total} · page {page + 1}/{total_pages}\n\n"
        "Pick a track to preview and download."
    )


def _track_card(track: dict[str, Any]) -> str:
    lines = [
        f"<b>{html.escape(str(track['title']))}</b>",
        html.escape(str(track["artist"])),
    ]
    if track.get("album"):
        lines.append(f"<i>{html.escape(str(track['album']))}</i>")

    details: list[str] = []
    if track.get("genre"):
        details.append(f"Genre: {html.escape(str(track['genre']))}")
    if track.get("duration"):
        details.append(f"Length: {_format_duration(int(track['duration']))}")
    if details:
        lines.extend(["", " · ".join(details)])

    lines.extend(["", "<i>Apple Music preview.</i>"])
    return "\n".join(lines)


def _history_text(entries: Sequence[dict[str, Any]]) -> str:
    lines = ["🕘 <b>Your recent downloads</b>", ""]
    for position, entry in enumerate(entries, start=1):
        lines.append(
            f"{position}. {html.escape(str(entry['title']))} — "
            f"{html.escape(str(entry['artist']))} "
            f"<i>({str(entry['created_at'])[:10]})</i>"
        )
    lines.extend(["", "Tap a track to get the audio again."])
    return "\n".join(lines)


async def _send_audio(
    bot: Bot,
    chat_id: int,
    audio: AudioService,
    track: dict[str, Any],
    reply_to_message_id: int | None = None,
) -> None:
    """Download (or reuse) the preview and upload it as a Telegram audio file."""
    path = await audio.prepare(str(track["id"]), str(track["preview_url"]))
    display_name = safe_filename(f"{track['artist']} - {track['title']}")
    kwargs: dict[str, Any] = {
        "chat_id": chat_id,
        "audio": FSInputFile(path, filename=f"{display_name}{path.suffix}"),
        "title": str(track["title"]),
        "performer": str(track["artist"]),
    }
    if reply_to_message_id is not None:
        kwargs["reply_to_message_id"] = reply_to_message_id

    artwork = await audio.prepare_artwork(str(track["id"]), str(track.get("artwork_url") or ""))
    if artwork is not None:
        try:
            await bot.send_audio(thumbnail=FSInputFile(artwork), **kwargs)
            return
        except TelegramBadRequest:
            log.warning("Telegram rejected the artwork for track %s, retrying without it", track["id"])
    await bot.send_audio(**kwargs)


async def _run_search(
    message: Message,
    state: FSMContext,
    search: MusicSearch,
    query: str,
    limit: int,
) -> None:
    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    try:
        tracks = await search.search(query, limit=limit)
    except SearchError:
        log.warning("Search failed for query %r", query, exc_info=True)
        await message.answer(SEARCH_ERROR_TEXT)
        return

    if not tracks:
        await message.answer(
            NO_RESULTS_TEXT.format(query=html.escape(query)),
            reply_markup=inline.search_prompt(),
        )
        return

    search_id = token_hex(4)
    serialised = [track.to_dict() for track in tracks]
    await state.set_data(
        {"search_id": search_id, "query": query, "results": serialised, "page": 0}
    )
    await state.set_state(None)
    await message.answer(
        _results_text(query, len(serialised), 0, PAGE_SIZE),
        reply_markup=inline.search_results(serialised, 0, PAGE_SIZE, search_id),
    )


async def _resolve_track(
    callback: CallbackQuery, state: FSMContext
) -> tuple[dict[str, Any] | None, str | None]:
    parts = (callback.data or "").split(":")
    if len(parts) != 3:
        return None, OUTDATED_TEXT
    _, search_id, raw_index = parts
    data = await state.get_data()
    if data.get("search_id") != search_id:
        return None, SEARCH_EXPIRED_TEXT

    tracks: list[dict[str, Any]] = data.get("results") or []
    try:
        index = int(raw_index)
    except ValueError:
        return None, OUTDATED_TEXT
    if not 0 <= index < len(tracks):
        return None, OUTDATED_TEXT
    return dict(tracks[index]), None


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    name = message.from_user.first_name if message.from_user else "there"
    await message.answer(_start_text(name), reply_markup=inline.main_menu())


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP_TEXT, reply_markup=inline.main_menu())


@router.message(Command("search"))
async def cmd_search(
    message: Message,
    command: CommandObject,
    state: FSMContext,
    search: MusicSearch,
    settings: Settings,
) -> None:
    query = (command.args or "").strip()
    if not query:
        await state.set_state(SearchStates.waiting_query)
        await message.answer(SEARCH_PROMPT, reply_markup=inline.search_prompt())
        return
    await _run_search(message, state, search, query, settings.search_limit)


@router.message(SearchStates.waiting_query, F.text)
async def on_search_query(
    message: Message,
    state: FSMContext,
    search: MusicSearch,
    settings: Settings,
) -> None:
    query = (message.text or "").strip()
    if len(query) < 2:
        await message.answer("Send at least two characters so I can search.")
        return
    await _run_search(message, state, search, query, settings.search_limit)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Cancelled.", reply_markup=inline.main_menu())


@router.message(Command("history"))
async def cmd_history(message: Message, db: Database) -> None:
    entries = [dict(row) for row in await db.get_history(message.from_user.id, limit=10)]
    if not entries:
        await message.answer(HISTORY_EMPTY_TEXT, reply_markup=inline.back_to_menu())
        return
    await message.answer(_history_text(entries), reply_markup=inline.history_list(entries))


@router.message(StateFilter(None), F.text, ~F.text.startswith("/"))
async def on_text_search(
    message: Message,
    state: FSMContext,
    search: MusicSearch,
    settings: Settings,
) -> None:
    query = (message.text or "").strip()
    if len(query) < 2:
        await message.answer("Send at least two characters so I can search.")
        return
    await _run_search(message, state, search, query, settings.search_limit)


@router.callback_query(F.data == "menu:search")
async def cb_menu_search(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(SearchStates.waiting_query)
    await callback.message.edit_text(SEARCH_PROMPT, reply_markup=inline.search_prompt())
    await callback.answer()


@router.callback_query(F.data == "menu:home")
async def cb_menu_home(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    name = callback.from_user.first_name or "there"
    await callback.message.edit_text(_start_text(name), reply_markup=inline.main_menu())
    await callback.answer()


@router.callback_query(F.data == "menu:help")
async def cb_menu_help(callback: CallbackQuery) -> None:
    await callback.message.edit_text(HELP_TEXT, reply_markup=inline.back_to_menu())
    await callback.answer()


@router.callback_query(F.data == "menu:history")
async def cb_menu_history(callback: CallbackQuery, db: Database) -> None:
    entries = [dict(row) for row in await db.get_history(callback.from_user.id, limit=10)]
    if not entries:
        await callback.message.edit_text(HISTORY_EMPTY_TEXT, reply_markup=inline.back_to_menu())
        await callback.answer()
        return
    await callback.message.edit_text(_history_text(entries), reply_markup=inline.history_list(entries))
    await callback.answer()


@router.callback_query(F.data == "close")
async def cb_close(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    try:
        await callback.message.delete()
    except TelegramBadRequest:
        await callback.message.edit_text("Closed.")
    await callback.answer()


@router.callback_query(F.data.startswith("s:"))
async def cb_results_page(callback: CallbackQuery, state: FSMContext) -> None:
    parts = (callback.data or "").split(":")
    if len(parts) != 3 or not parts[2].isdigit():
        await callback.answer(OUTDATED_TEXT, show_alert=True)
        return

    search_id, raw_page = parts[1], int(parts[2])
    data = await state.get_data()
    if data.get("search_id") != search_id:
        await callback.answer(SEARCH_EXPIRED_TEXT, show_alert=True)
        return

    tracks: list[dict[str, Any]] = data.get("results") or []
    total_pages = max(1, (len(tracks) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(raw_page, total_pages - 1))
    await state.update_data(page=page)
    await callback.message.edit_text(
        _results_text(str(data.get("query", "")), len(tracks), page, PAGE_SIZE),
        reply_markup=inline.search_results(tracks, page, PAGE_SIZE, search_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("t:"))
async def cb_track_details(callback: CallbackQuery, state: FSMContext) -> None:
    track, error = await _resolve_track(callback, state)
    if track is None:
        await callback.answer(error or OUTDATED_TEXT, show_alert=True)
        return

    parts = (callback.data or "").split(":")
    data = await state.get_data()
    await callback.message.edit_text(
        _track_card(track),
        reply_markup=inline.track_actions(parts[1], int(parts[2]), int(data.get("page", 0))),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("d:"))
async def cb_download(
    callback: CallbackQuery,
    state: FSMContext,
    audio: AudioService,
    db: Database,
) -> None:
    track, error = await _resolve_track(callback, state)
    if track is None:
        await callback.answer(error or OUTDATED_TEXT, show_alert=True)
        return

    await callback.answer("Preparing audio…")
    await callback.bot.send_chat_action(callback.message.chat.id, ChatAction.UPLOAD_DOCUMENT)
    try:
        await _send_audio(
            callback.bot,
            callback.message.chat.id,
            audio,
            track,
            reply_to_message_id=callback.message.message_id,
        )
    except AudioError as exc:
        log.warning("Could not prepare track %s: %s", track["id"], exc)
        await callback.message.answer(AUDIO_ERROR_TEXT)
        return
    except TelegramAPIError:
        log.exception("Telegram refused the audio for track %s", track["id"])
        await callback.message.answer(AUDIO_ERROR_TEXT)
        return

    await db.add_download(
        user_id=callback.from_user.id,
        track_id=str(track["id"]),
        title=str(track["title"]),
        artist=str(track["artist"]),
        preview_url=str(track["preview_url"]),
        artwork_url=track.get("artwork_url"),
    )


@router.callback_query(F.data.startswith("h:"))
async def cb_history_entry(callback: CallbackQuery, audio: AudioService, db: Database) -> None:
    raw_id = (callback.data or "").split(":", 1)[-1]
    if not raw_id.isdigit():
        await callback.answer(OUTDATED_TEXT, show_alert=True)
        return

    entry = await db.get_download(callback.from_user.id, int(raw_id))
    if entry is None:
        await callback.answer("This download is no longer in your history.", show_alert=True)
        return

    track = {
        "id": entry["track_id"],
        "title": entry["title"],
        "artist": entry["artist"],
        "preview_url": entry["preview_url"],
        "artwork_url": entry["artwork_url"] or "",
    }
    await callback.answer("Preparing audio…")
    await callback.bot.send_chat_action(callback.message.chat.id, ChatAction.UPLOAD_DOCUMENT)
    try:
        await _send_audio(
            callback.bot,
            callback.message.chat.id,
            audio,
            track,
            reply_to_message_id=callback.message.message_id,
        )
    except AudioError as exc:
        log.warning("Could not prepare history entry %s: %s", raw_id, exc)
        await callback.message.answer(AUDIO_ERROR_TEXT)
    except TelegramAPIError:
        log.exception("Telegram refused the audio for history entry %s", raw_id)
        await callback.message.answer(AUDIO_ERROR_TEXT)


@router.message(F.text.startswith("/"))
async def unknown_command(message: Message) -> None:
    await message.answer(
        "I don't know that command. Use /help to see what I can do.",
        reply_markup=inline.main_menu(),
    )


@router.callback_query()
async def cb_unknown(callback: CallbackQuery) -> None:
    await callback.answer(OUTDATED_TEXT)
