"""Entry point for the Telegram music bot."""

from __future__ import annotations

import asyncio
import logging
import sys

import aiohttp
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, ErrorEvent
from aiohttp import web
from pydantic import ValidationError

from config import Settings, get_settings
from database.db import Database
from handlers import admin, user
from handlers.user import UserMiddleware
from services.audio import AudioService
from services.search import MusicSearch

log = logging.getLogger(__name__)

COMMANDS = [
    BotCommand(command="start", description="Botni ishga tushirish"),
    BotCommand(command="search", description="Qo‘shiq qidirish"),
    BotCommand(command="history", description="Oxirgi yuklamalarim"),
    BotCommand(command="help", description="Yordam"),
]


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)


def create_health_app() -> web.Application:
    app = web.Application()

    async def health(_: web.Request) -> web.Response:
        return web.json_response({"status": "ok", "service": "telegram-music-bot"})

    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    return app


async def start_health_server(port: int) -> web.AppRunner:
    """Serve a tiny health endpoint so hosting platforms can keep the bot awake."""
    runner = web.AppRunner(create_health_app(), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()
    log.info("Health server listening on 0.0.0.0:%d", port)
    return runner


def create_dispatcher(
    settings: Settings,
    database: Database,
    search: MusicSearch,
    audio: AudioService,
) -> Dispatcher:
    """Build the dispatcher with middleware, workflow data and routers."""
    dispatcher = Dispatcher(storage=MemoryStorage())

    middleware = UserMiddleware(database)
    dispatcher.message.middleware(middleware)
    dispatcher.callback_query.middleware(middleware)

    dispatcher["settings"] = settings
    dispatcher["db"] = database
    dispatcher["search"] = search
    dispatcher["audio"] = audio

    # Admin routes first: their commands are hidden from everyone else and fall
    # through to the user router, which treats them as unknown commands.
    dispatcher.include_routers(admin.router, user.router)

    @dispatcher.errors()
    async def on_error(event: ErrorEvent) -> bool:
        log.error("Unhandled error while processing an update", exc_info=event.exception)
        return True

    return dispatcher


async def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    settings.ensure_directories()

    database = Database(settings.db_path)
    await database.connect()

    session = aiohttp.ClientSession()
    search = MusicSearch(
        session,
        country=settings.itunes_country,
        timeout=settings.request_timeout,
    )
    audio = AudioService(
        session,
        settings.audio_dir,
        cache_ttl=settings.audio_cache_ttl,
        max_size=settings.max_audio_size,
    )

    removed = await audio.cleanup()
    if removed:
        log.info("Removed %d expired cached audio file(s)", removed)

    bot = Bot(
        settings.bot_token,
        default=DefaultBotProperties(
            parse_mode=ParseMode.HTML,
            link_preview_is_disabled=True,
        ),
    )
    dispatcher = create_dispatcher(settings, database, search, audio)

    health_runner: web.AppRunner | None = None
    try:
        if settings.port:
            health_runner = await start_health_server(settings.port)
        await bot.set_my_commands(COMMANDS)
        me = await bot.get_me()
        log.info("Starting @%s for %d admin(s)", me.username, len(settings.admin_ids))
        await dispatcher.start_polling(
            bot,
            allowed_updates=dispatcher.resolve_used_update_types(),
        )
    finally:
        if health_runner is not None:
            await health_runner.cleanup()
        await database.close()
        await session.close()
        await bot.session.close()
        log.info("Bot stopped")


def run() -> None:
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except ValidationError as exc:
        print(f"Configuration error:\n{exc}", file=sys.stderr)
        sys.exit(1)
    except TelegramUnauthorizedError:
        print(
            "Telegram rejected BOT_TOKEN. Check the value in your .env file.",
            file=sys.stderr,
        )
        sys.exit(1)


if __name__ == "__main__":
    run()
