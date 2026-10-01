import os

import pymupdf
import pytest
from redis import Redis
from redis.exceptions import ConnectionError

# Database 15 keeps test data apart from development data in database 0.
TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/15")


@pytest.fixture
def redis():
    client = Redis.from_url(TEST_REDIS_URL, decode_responses=True)
    try:
        client.ping()
    except ConnectionError:
        pytest.fail(
            f"Redis is not reachable at {TEST_REDIS_URL}. "
            "Start it with: docker compose up -d --wait"
        )
    client.flushdb()
    yield client
    client.flushdb()
    client.close()


def _make_pdf(pages: list[str]) -> bytes:
    """Create a PDF in memory with one page per string ("" = blank page)."""
    with pymupdf.open() as doc:
        for text in pages:
            page = doc.new_page()
            if text:
                page.insert_text((72, 72), text)
        return doc.tobytes()


@pytest.fixture
def make_pdf():
    """Factory fixture: tests call make_pdf(["page 1 text", "page 2 text"])."""
    return _make_pdf
