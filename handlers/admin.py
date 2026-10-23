"""Admin handlers: statistics, broadcast and user management."""

from __future__ import annotations

import asyncio
import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiogram.filters import BaseFilter, Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, TelegramObject

from config import get_settings
from database.db import Database
from keyboards import inline

log = logging.getLogger(__name__)

router = Router(name="admin")
BROADCAST_DELAY = 0.05


class IsAdmin(BaseFilter):
    """Only allows users listed in ADMIN_IDS."""

    async def __call__(self, event: TelegramObject) -> bool:
        user = getattr(event, "from_user", None)
        return user is not None and user.id in get_settings().admin_ids


# Applied to every handler in this router, so admin commands fall through to the
# user router for everyone else.
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())


class BroadcastStates(StatesGroup):
    waiting_content = State()
    confirming = State()


def _stats_text(stats: dict) -> str:
    return (
        "<b>Admin panel</b>\n\n"
        f"👥 Users: <b>{stats['users']}</b> (new today: {stats['new_today']})\n"
        f"🚫 Banned: <b>{stats['banned']}</b>\n"
        f"⬇️ Downloads: <b>{stats['downloads']}</b> (today: {stats['downloads_today']})\n\n"
        "<b>Commands</b>\n"
        "/broadcast – message every user\n"
        "/ban &lt;id | reply&gt; – block a user\n"
        "/unban &lt;id | reply&gt; – unblock a user"
    )


def _target_from(message: Message, command: CommandObject) -> int | None:
    reply = message.reply_to_message
    if reply and reply.from_user and not reply.from_user.is_bot:
        return reply.from_user.id
    args = (command.args or "").strip()
    if args.lstrip("-").isdigit():
        return int(args)
    return None


async def _send_admin_panel(message: Message, db: Database) -> None:
    stats = await db.stats()
    await message.answer(_stats_text(stats), reply_markup=inline.admin_panel())


@router.message(Command("admin", "stats"))
async def cmd_admin(message: Message, db: Database) -> None:
    await _send_admin_panel(message, db)


@router.callback_query(F.data == "admin:refresh")
async def cb_admin_refresh(callback: CallbackQuery, db: Database) -> None:
    stats = await db.stats()
    await callback.message.edit_text(_stats_text(stats), reply_markup=inline.admin_panel())
    await callback.answer("Refreshed")


@router.message(Command("cancel"))
async def cmd_cancel_broadcast(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Broadcast cancelled.")


@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message, state: FSMContext) -> None:
    await state.set_state(BroadcastStates.waiting_content)
    await message.answer(
        "📢 Send the message to broadcast to all users. It can be text, a photo, "
        "a video or a forwarded message — one message at a time.\n\nUse /cancel to abort.",
        reply_markup=inline.broadcast_prompt(),
    )


@router.callback_query(F.data == "admin:broadcast")
async def cb_admin_broadcast(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(BroadcastStates.waiting_content)
    await callback.message.edit_text(
        "📢 Send the message to broadcast to all users. It can be text, a photo, "
        "a video or a forwarded message — one message at a time.\n\nUse /cancel to abort.",
        reply_markup=inline.broadcast_prompt(),
    )
    await callback.answer()


@router.message(BroadcastStates.waiting_content)
async def broadcast_content(message: Message, state: FSMContext, db: Database) -> None:
    recipients = len(await db.all_user_ids())
    await state.set_data(
        {"source_chat_id": message.chat.id, "source_message_id": message.message_id}
    )
    await state.set_state(BroadcastStates.confirming)
    await message.answer(
        f"Ready to send this message to <b>{recipients}</b> user(s)?",
        reply_markup=inline.broadcast_confirm(recipients),
    )


@router.callback_query(F.data == "bc:cancel")
async def cb_broadcast_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text("Broadcast cancelled.")
    await callback.answer()


@router.callback_query(F.data == "bc:start")
async def cb_broadcast_start(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    data = await state.get_data()
    source_chat_id = data.get("source_chat_id")
    source_message_id = data.get("source_message_id")
    if not source_chat_id or not source_message_id:
        await state.clear()
        await callback.answer("Nothing to broadcast. Start with /broadcast.", show_alert=True)
        return

    user_ids = await db.all_user_ids()
    await state.clear()
    await callback.answer("Broadcast started")
    await callback.message.edit_text(f"📤 Sending to {len(user_ids)} user(s)…")

    sent = 0
    failed = 0
    for user_id in user_ids:
        try:
            await callback.bot.copy_message(
                chat_id=user_id,
                from_chat_id=source_chat_id,
                message_id=source_message_id,
            )
            sent += 1
        except TelegramRetryAfter as exc:
            await asyncio.sleep(exc.retry_after)
            try:
                await callback.bot.copy_message(
                    chat_id=user_id,
                    from_chat_id=source_chat_id,
                    message_id=source_message_id,
                )
                sent += 1
            except TelegramAPIError:
                failed += 1
        except TelegramAPIError:
            failed += 1
        await asyncio.sleep(BROADCAST_DELAY)

    log.info("Broadcast finished: %d delivered, %d failed", sent, failed)
    await callback.message.edit_text(
        f"✅ Broadcast finished.\n\nDelivered: <b>{sent}</b>\nFailed: <b>{failed}</b>"
    )


@router.message(Command("ban"))
async def cmd_ban(message: Message, command: CommandObject, db: Database) -> None:
    target = _target_from(message, command)
    if target is None:
        await message.answer("Usage: <code>/ban &lt;user_id&gt;</code> or reply to a user's message with /ban.")
        return

    settings = get_settings()
    if target in settings.admin_ids or target == message.from_user.id:
        await message.answer("Admins cannot be banned.")
        return
    if message.bot and target == message.bot.id:
        await message.answer("I can't ban myself.")
        return

    if not await db.set_banned(target, True):
        await message.answer(
            f"User <code>{target}</code> is not in the database yet, so there is nothing to ban."
        )
        return

    log.info("Admin %s banned user %s", message.from_user.id, target)
    await message.answer(f"🚫 User <code>{target}</code> has been banned.")


@router.message(Command("unban"))
async def cmd_unban(message: Message, command: CommandObject, db: Database) -> None:
    target = _target_from(message, command)
    if target is None:
        await message.answer("Usage: <code>/unban &lt;user_id&gt;</code> or reply to a user's message with /unban.")
        return

    if not await db.set_banned(target, False):
        await message.answer(f"User <code>{target}</code> is not in the database.")
        return

    log.info("Admin %s unbanned user %s", message.from_user.id, target)
    await message.answer(f"✅ User <code>{target}</code> has been unbanned.")
