"""Redis connection setup shared by all components."""

import os

from redis import Redis

DEFAULT_REDIS_URL = "redis://localhost:6379/0"


def connect(url: str | None = None) -> Redis:
    """Connect to Redis at url, else $REDIS_URL, else DEFAULT_REDIS_URL.

    Responses are decoded to str, which search() relies on.
    """
    url = url or os.environ.get("REDIS_URL", DEFAULT_REDIS_URL)
    return Redis.from_url(url, decode_responses=True)
