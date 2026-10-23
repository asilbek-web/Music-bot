"""Music catalog search backed by the public iTunes Search API (no API key required)."""

from __future__ import annotations

import asyncio
import logging
import unicodedata
from dataclasses import asdict, dataclass
from typing import Any

import aiohttp

log = logging.getLogger(__name__)

ITUNES_SEARCH_URL = "https://itunes.apple.com/search"
MAX_FETCH = 50

# Words that show up in upload titles but rarely in what the user typed. They are
# penalised so an official recording outranks karaoke, covers and remixes.
NOISE_WORDS = frozenset(
    {
        "official",
        "video",
        "music",
        "audio",
        "lyrics",
        "lyric",
        "feat",
        "ft",
        "remix",
        "remastered",
        "remaster",
        "version",
        "clip",
        "hd",
        "4k",
        "topic",
        "prod",
        "instrumental",
        "karaoke",
        "cover",
        "tribute",
        "piano",
        "guitar",
        "lullaby",
        "nursery",
        "rhymes",
        "babies",
        "baby",
        "sleep",
        "edit",
        "slowed",
        "reverb",
        "sped",
    }
)

_CYRILLIC = {
    "а": "a",
    "б": "b",
    "в": "v",
    "г": "g",
    "ғ": "g",
    "д": "d",
    "е": "e",
    "ё": "yo",
    "ж": "j",
    "з": "z",
    "и": "i",
    "й": "y",
    "к": "k",
    "қ": "q",
    "л": "l",
    "м": "m",
    "н": "n",
    "о": "o",
    "п": "p",
    "р": "r",
    "с": "s",
    "т": "t",
    "у": "u",
    "ў": "o",
    "ф": "f",
    "х": "x",
    "ҳ": "h",
    "ц": "ts",
    "ч": "ch",
    "ш": "sh",
    "ъ": "",
    "ь": "",
    "ы": "i",
    "э": "e",
    "ю": "yu",
    "я": "ya",
}


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


def _transliterate(text: str) -> str:
    """Map Cyrillic (Uzbek/Russian) letters to their Latin equivalents."""
    return "".join(_CYRILLIC.get(char, char) for char in text.lower())


def _strip_marks(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def normalise(text: str) -> str:
    """Lowercase, transliterate accents/Cyrillic and drop punctuation."""
    text = _strip_marks(_transliterate(text)).lower()
    cleaned = "".join(char if char.isalnum() else " " for char in text)
    return " ".join(cleaned.split())


def _turkify(text: str) -> str:
    """Rewrite Uzbek spelling so it lines up with Turkish/Latin catalog titles."""
    text = text.replace("ch", "c").replace("sh", "s")
    text = text.replace("q", "k").replace("x", "h")
    return " ".join(text.split())


def query_variants(query: str) -> list[str]:
    """All spellings of the query worth matching against the catalog."""
    base = normalise(query)
    if not base:
        return []
    variants = [base]
    alternative = _turkify(base)
    if alternative and alternative not in variants:
        variants.append(alternative)
    return variants


def _score(variants: list[str], title: str, artist: str, album: str) -> float:
    """How well a catalog entry matches the query, across every spelling."""
    norm_title = normalise(title)
    norm_artist = normalise(artist)
    norm_album = normalise(album)
    title_tokens = set(norm_title.split())
    artist_tokens = set(norm_artist.split())
    best = 0.0

    for query in variants:
        tokens = query.split()
        if not tokens:
            continue

        score = 0.0
        if norm_title == query:
            score += 1000
        elif norm_title.startswith(query):
            score += 420
        elif query in norm_title:
            score += 260

        if norm_artist == query:
            score += 340
        elif query in norm_artist:
            score += 200

        matched = 0
        for token in tokens:
            if token in title_tokens:
                matched += 1
                score += 90
            elif token in norm_title:
                matched += 1
                score += 70
            elif token in artist_tokens:
                matched += 1
                score += 55
            elif token in norm_artist:
                matched += 1
                score += 40
            elif token in norm_album:
                matched += 0.5
                score += 15

        ratio = matched / len(tokens)
        if ratio == 1.0:
            score += 160
        elif ratio == 0:
            score -= 130
        else:
            score -= 130 * (1 - ratio)

        # A cover usually repeats the whole query in its title but has an
        # unrelated artist. Boosting the artist makes the real recording win.
        artist_ratio = sum(1 for token in tokens if token in artist_tokens or token in norm_artist) / len(tokens)
        score += 300 * artist_ratio
        if artist_ratio == 0 and len(tokens) > 1:
            score -= 250

        # Prefer a compact title over a long remix/live/lyrics variant.
        extra_words = len(title_tokens) - len(tokens)
        if extra_words > 0:
            score -= min(300, extra_words * 120)

        # Bootleg uploads repeat the artist name inside the title
        # ("Ed Sheeran - Perfect (Remix)" by some third party). The real
        # recording keeps them separate, so penalise the repetition.
        if len(tokens) > 1:
            repeated = sum(1 for token in title_tokens if token in artist_tokens)
            if repeated:
                score -= min(240, repeated * 120)

        best = max(best, score)

    query_words = set(" ".join(variants).split())
    catalogue_words = title_tokens | artist_tokens
    penalties = sum(
        1 for word in catalogue_words if word in NOISE_WORDS and word not in query_words
    )
    if penalties:
        best -= min(280, penalties * 140)

    return best


def _dedupe_key(track: Track) -> tuple[str, str]:
    return normalise(track.title), normalise(track.artist)


class MusicSearch:
    """Queries the Apple Music catalog and ranks the results by relevance."""

    def __init__(self, session: aiohttp.ClientSession, *, country: str = "US", timeout: float = 20.0) -> None:
        self._session = session
        self._country = country.upper()
        self._timeout = timeout

    async def search(self, query: str, limit: int = 20) -> list[Track]:
        query = " ".join(query.split())[:200]
        variants = query_variants(query)
        if not query or not variants:
            return []

        fetch_limit = min(MAX_FETCH, max(25, limit * 2))
        terms: list[str] = []
        for term in [query, *variants]:
            normalised = normalise(term)
            if normalised and normalised not in {normalise(existing) for existing in terms}:
                terms.append(term)

        responses = await asyncio.gather(
            *(self._fetch(term, fetch_limit) for term in terms),
            return_exceptions=True,
        )

        items: list[dict[str, Any]] = []
        errors = 0
        for response in responses:
            if isinstance(response, BaseException):
                errors += 1
                log.debug("Catalog request failed: %s", response)
                continue
            items.extend(response)

        if errors == len(responses):
            raise SearchError("The music catalog is unreachable")

        tracks: list[Track] = []
        seen_ids: set[str] = set()
        for item in items:
            try:
                track = Track.from_itunes(item)
            except (KeyError, TypeError, ValueError):
                log.debug("Skipping unusable search result: %r", item)
                continue
            if track.id in seen_ids:
                continue
            seen_ids.add(track.id)
            tracks.append(track)

        scored = [
            (score, track)
            for track in tracks
            if (score := _score(variants, track.title, track.artist, track.album)) > 0
        ]
        scored.sort(key=lambda pair: pair[0], reverse=True)

        ranked: list[Track] = []
        seen_keys: set[tuple[str, str]] = set()
        for _, track in scored:
            key = _dedupe_key(track)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            ranked.append(track)
            if len(ranked) >= limit:
                break
        return ranked

    async def _fetch(self, term: str, limit: int) -> list[dict[str, Any]]:
        params = {
            "term": term,
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

        results = payload.get("results")
        if not isinstance(results, list):
            return []
        return [item for item in results if isinstance(item, dict)]
