"""Music catalog search backed by the public iTunes Search API (no API key required)."""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from typing import Any

import aiohttp

log = logging.getLogger(__name__)

ITUNES_SEARCH_URL = "https://itunes.apple.com/search"


class SearchError(RuntimeError):
    """Raised when the catalog cannot be queried."""


@dataclass(frozen=True, slots=True)
class Track:
    """A single search result with a playable preview."""

    id: str
    title: str
    artist: str
    album: str
    genre: str
    duration: int
    preview_url: str
    artwork_url: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Track:
        return cls(
            id=str(data["id"]),
            title=data["title"],
            artist=data["artist"],
            album=data.get("album", ""),
            genre=data.get("genre", ""),
            duration=int(data.get("duration", 0)),
            preview_url=data["preview_url"],
            artwork_url=data.get("artwork_url", ""),
        )

    @classmethod
    def from_itunes(cls, item: dict[str, Any]) -> Track:
        preview_url = item.get("previewUrl")
        if not preview_url:
            raise ValueError("track has no preview")
        artwork = item.get("artworkUrl100") or ""
        if artwork:
            # iTunes serves the same artwork at several sizes. 300x300 fits the
            # limits Telegram applies to audio thumbnails (JPEG, under 200 kB).
            artwork = artwork.replace("100x100bb", "300x300bb")
        return cls(
            id=str(item["trackId"]),
            title=item.get("trackName") or "Unknown title",
            artist=item.get("artistName") or "Unknown artist",
            album=item.get("collectionName") or "",
            genre=item.get("primaryGenreName") or "",
            duration=int(item.get("trackTimeMillis") or 0) // 1000,
            preview_url=preview_url,
            artwork_url=artwork,
        )


class MusicSearch:
    """Queries the Apple Music catalog for songs with a 30-second preview."""

    def __init__(self, session: aiohttp.ClientSession, *, country: str = "US", timeout: float = 20.0) -> None:
        self._session = session
        self._country = country.upper()
        self._timeout = timeout

    async def search(self, query: str, limit: int = 10) -> list[Track]:
        query = " ".join(query.split())[:200]
        if not query:
            return []

        params = {
            "term": query,
            "media": "music",
            "entity": "song",
            "limit": str(limit),
            "country": self._country,
        }
        try:
            async with self._session.get(
                ITUNES_SEARCH_URL,
                params=params,
                timeout=aiohttp.ClientTimeout(total=self._timeout),
            ) as response:
                if response.status != 200:
                    raise SearchError(f"iTunes returned HTTP {response.status}")
                # The API answers with a JavaScript content type, so decoding is forced.
                payload = await response.json(content_type=None)
        except TimeoutError as exc:
            raise SearchError("The music catalog timed out") from exc
        except aiohttp.ClientError as exc:
            raise SearchError("The music catalog is unreachable") from exc
        except ValueError as exc:  # includes JSON decode errors
            raise SearchError("The music catalog returned an invalid response") from exc

        if not isinstance(payload, dict):
            raise SearchError("The music catalog returned an unexpected payload")

        tracks: list[Track] = []
        for item in payload.get("results") or []:
            try:
                tracks.append(Track.from_itunes(item))
            except (KeyError, TypeError, ValueError):
                log.debug("Skipping unusable search result: %r", item)
        return tracks
