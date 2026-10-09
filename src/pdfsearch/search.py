"""Answering substring queries from the Redis trigram index."""

import time
from dataclasses import dataclass

from redis import Redis

from pdfsearch.index import page_key, parse_page_id, trigram_key
from pdfsearch.text import TRIGRAM_LENGTH, normalize, trigrams

# A shorter query has no trigrams, so the index could not narrow it down.
MIN_QUERY_LENGTH = TRIGRAM_LENGTH


class QueryTooShortError(ValueError):
    """The normalized query is shorter than MIN_QUERY_LENGTH."""


@dataclass(frozen=True, order=True)
class PageMatch:
    """A page containing the query. Sorts by file, then page number."""

    file: str
    page: int


@dataclass
class SearchStats:
    """How a search went, e.g. for metrics."""

    # Pages containing every trigram of the query, and pages that really
    # contain it; candidates - matches are the trigram filter's false positives.
    candidates: int = 0
    matches: int = 0
    intersect_seconds: float = 0.0
    verify_seconds: float = 0.0


def search(redis: Redis, query: str, *, stats: SearchStats | None = None) -> list[PageMatch]:
    """Return every page whose normalized text contains the normalized query.

    The Redis client must be created with decode_responses=True. If stats is
    given, it is filled in with candidate and match counts and phase timings.

    Raises:
        QueryTooShortError: if the normalized query is shorter than
            MIN_QUERY_LENGTH.
    """
    normalized = normalize(query)
    if len(normalized) < MIN_QUERY_LENGTH:
        raise QueryTooShortError(
            f"query must have at least {MIN_QUERY_LENGTH} characters after normalization"
        )

    # Candidates: pages containing every trigram of the query. This can
    # include pages that do not contain the query itself, but never misses one.
    stats = SearchStats() if stats is None else stats
    start = time.perf_counter()
    keys = [trigram_key(t) for t in trigrams(normalized)]
    candidates = list(redis.sinter(keys))
    intersected = time.perf_counter()
    stats.candidates = len(candidates)
    stats.intersect_seconds = intersected - start
    if not candidates:
        return []

    # Verification: fetch all candidate texts in one round trip and keep the
    # pages that really contain the query.
    texts = redis.mget([page_key(pid) for pid in candidates])
    matches = [
        PageMatch(*parse_page_id(pid))
        for pid, text in zip(candidates, texts, strict=True)
        # text is None only if the page key was removed by hand; skip it.
        if text is not None and normalized in text
    ]
    stats.matches = len(matches)
    stats.verify_seconds = time.perf_counter() - intersected
    return sorted(matches)
