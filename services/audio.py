"""Downloading and caching of audio previews before they are uploaded to Telegram."""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from urllib.parse import urlparse

import aiohttp

log = logging.getLogger(__name__)

_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]+")
AUDIO_SUFFIX = ".m4a"
ARTWORK_SUFFIX = ".jpg"
MIN_AUDIO_BYTES = 1024
MIN_ARTWORK_BYTES = 512
MAX_ARTWORK_BYTES = 200 * 1024


class AudioError(RuntimeError):
    """Raised when the audio file cannot be downloaded or stored."""


def safe_filename(name: str, fallback: str = "track", max_length: int = 80) -> str:
    """Turn arbitrary track metadata into a safe file name."""
    cleaned = _UNSAFE_CHARS.sub("_", name).strip("._-")
    return cleaned[:max_length] or fallback


class AudioService:
    """Fetches preview files and keeps a small, expiring on-disk cache."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        directory: Path | str,
        *,
        cache_ttl: int = 86_400,
        max_size: int = 20 * 1024 * 1024,
        timeout: float = 60.0,
    ) -> None:
        self._session = session
        self._dir = Path(directory)
        self._cache_ttl = cache_ttl
        self._max_size = max_size
        self._timeout = timeout

    def cached_path(self, track_id: str) -> Path:
        return self._dir / f"{safe_filename(track_id, fallback='track', max_length=64)}{AUDIO_SUFFIX}"

    def cached_artwork_path(self, track_id: str) -> Path:
        return self._dir / f"{safe_filename(track_id, fallback='track', max_length=64)}{ARTWORK_SUFFIX}"

    async def prepare(self, track_id: str, preview_url: str) -> Path:
        """Return a local path for the preview, downloading it when not cached."""
        if urlparse(preview_url).scheme not in {"http", "https"}:
            raise AudioError("Unsupported audio URL")

        path = self.cached_path(track_id)
        if self._is_fresh(path):
            log.debug("Using cached audio for track %s", track_id)
            return path

        self._dir.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(".part")
        downloaded = 0

        try:
            async with self._session.get(
                preview_url,
                timeout=aiohttp.ClientTimeout(total=self._timeout),
            ) as response:
                if response.status != 200:
                    raise AudioError(f"Audio download failed with HTTP {response.status}")
                with temp_path.open("wb") as handle:
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        downloaded += len(chunk)
                        if downloaded > self._max_size:
                            raise AudioError("Audio file exceeds the configured size limit")
                        handle.write(chunk)

            if downloaded < MIN_AUDIO_BYTES:
                raise AudioError("Downloaded audio file is incomplete")
            temp_path.replace(path)
        except AudioError:
            temp_path.unlink(missing_ok=True)
            raise
        except (TimeoutError, aiohttp.ClientError, OSError) as exc:
            temp_path.unlink(missing_ok=True)
            raise AudioError("Could not download the audio file") from exc

        log.info("Downloaded preview %s (%d KiB)", path.name, downloaded // 1024)
        return path

    async def prepare_artwork(self, track_id: str, artwork_url: str) -> Path | None:
        """Return a cached JPEG cover for the track, or None when unavailable."""
        if not artwork_url or urlparse(artwork_url).scheme not in {"http", "https"}:
            return None

        path = self.cached_artwork_path(track_id)
        if self._is_fresh(path):
            return path

        self._dir.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(f"{path.name}.part")
        try:
            async with self._session.get(
                artwork_url,
                timeout=aiohttp.ClientTimeout(total=self._timeout),
            ) as response:
                if response.status != 200:
                    return None
                data = await response.content.read(MAX_ARTWORK_BYTES + 1)
            if not MIN_ARTWORK_BYTES <= len(data) <= MAX_ARTWORK_BYTES:
                return None
            temp_path.write_bytes(data)
            temp_path.replace(path)
        except (TimeoutError, aiohttp.ClientError, OSError):
            temp_path.unlink(missing_ok=True)
            log.warning("Could not download artwork for track %s", track_id, exc_info=True)
            return None

        return path

    async def cleanup(self) -> int:
        """Delete leftover partial downloads and expired cache entries."""
        if not self._dir.exists():
            return 0

        removed = 0
        for path in self._dir.iterdir():
            if not path.is_file():
                continue
            if path.suffix == ".part" or not self._is_fresh(path):
                try:
                    path.unlink()
                    removed += 1
                except OSError:
                    log.warning("Could not remove cached file %s", path, exc_info=True)
        return removed

    def _is_fresh(self, path: Path, *, now: float | None = None) -> bool:
        try:
            stat = path.stat()
        except OSError:
            return False
        if stat.st_size == 0:
            return False
        if self._cache_ttl <= 0:
            return True
        now = time.time() if now is None else now
        return (now - stat.st_mtime) < self._cache_ttl
