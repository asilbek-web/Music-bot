"""SQLite persistence for users, bans and download history."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY,
    username      TEXT,
    full_name     TEXT NOT NULL DEFAULT '',
    language_code TEXT,
    is_banned     INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    last_seen     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS downloads (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    track_id    TEXT NOT NULL,
    title       TEXT NOT NULL,
    artist      TEXT NOT NULL,
    preview_url TEXT NOT NULL,
    artwork_url TEXT,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_downloads_user ON downloads (user_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_users_banned ON users (is_banned);
"""


def utcnow() -> str:
    """Current UTC timestamp in a sortable ISO-8601 form."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def start_of_today() -> str:
    """UTC midnight of the current day, used by the statistics queries."""
    midnight = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.isoformat(timespec="seconds")


class Database:
    """Thin async wrapper around a single SQLite connection."""

    def __init__(self, path: Path | str) -> None:
        self._path = str(path)
        self._conn: aiosqlite.Connection | None = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database.connect() must be called before using the database")
        return self._conn

    async def connect(self) -> None:
        if self._conn is not None:
            return
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()
        log.info("Database ready at %s", self._path)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
            log.info("Database connection closed")

    async def upsert_user(
        self,
        user_id: int,
        username: str | None,
        full_name: str,
        language_code: str | None = None,
    ) -> None:
        """Insert a user on first contact and refresh their profile data afterwards."""
        now = utcnow()
        await self.conn.execute(
            """
            INSERT INTO users (id, username, full_name, language_code, created_at, last_seen)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                username = excluded.username,
                full_name = excluded.full_name,
                language_code = excluded.language_code,
                last_seen = excluded.last_seen
            """,
            (user_id, username, full_name, language_code, now, now),
        )
        await self.conn.commit()

    async def get_user(self, user_id: int) -> aiosqlite.Row | None:
        async with self.conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)) as cursor:
            return await cursor.fetchone()

    async def is_banned(self, user_id: int) -> bool:
        async with self.conn.execute("SELECT is_banned FROM users WHERE id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
        return bool(row and row["is_banned"])

    async def set_banned(self, user_id: int, banned: bool) -> bool:
        """Ban or unban a known user. Returns False when the user has never used the bot."""
        cursor = await self.conn.execute(
            "UPDATE users SET is_banned = ? WHERE id = ?",
            (1 if banned else 0, user_id),
        )
        await self.conn.commit()
        changed = cursor.rowcount > 0
        await cursor.close()
        return changed

    async def add_download(
        self,
        user_id: int,
        track_id: str,
        title: str,
        artist: str,
        preview_url: str,
        artwork_url: str | None = None,
    ) -> int:
        cursor = await self.conn.execute(
            """
            INSERT INTO downloads (user_id, track_id, title, artist, preview_url, artwork_url, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, track_id, title, artist, preview_url, artwork_url, utcnow()),
        )
        await self.conn.commit()
        entry_id = cursor.lastrowid or 0
        await cursor.close()
        return entry_id

    async def get_history(self, user_id: int, limit: int = 10) -> list[aiosqlite.Row]:
        async with self.conn.execute(
            "SELECT * FROM downloads WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ) as cursor:
            return list(await cursor.fetchall())

    async def get_download(self, user_id: int, entry_id: int) -> aiosqlite.Row | None:
        async with self.conn.execute(
            "SELECT * FROM downloads WHERE id = ? AND user_id = ?",
            (entry_id, user_id),
        ) as cursor:
            return await cursor.fetchone()

    async def all_user_ids(self) -> list[int]:
        """Non-banned users, used for broadcasts."""
        async with self.conn.execute("SELECT id FROM users WHERE is_banned = 0 ORDER BY id") as cursor:
            return [row["id"] for row in await cursor.fetchall()]

    async def stats(self) -> dict[str, Any]:
        today = start_of_today()
        async with self.conn.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM users) AS users,
                (SELECT COUNT(*) FROM users WHERE is_banned = 1) AS banned,
                (SELECT COUNT(*) FROM users WHERE created_at >= ?) AS new_today,
                (SELECT COUNT(*) FROM downloads) AS downloads,
                (SELECT COUNT(*) FROM downloads WHERE created_at >= ?) AS downloads_today
            """,
            (today, today),
        ) as cursor:
            row = await cursor.fetchone()
        return dict(row) if row else {
            "users": 0,
            "banned": 0,
            "new_today": 0,
            "downloads": 0,
            "downloads_today": 0,
        }
