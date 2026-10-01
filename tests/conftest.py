import os

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
