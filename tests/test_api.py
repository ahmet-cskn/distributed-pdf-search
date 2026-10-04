import pytest
from fastapi.testclient import TestClient

from pdfsearch.api import app, get_redis
from pdfsearch.db import connect
from pdfsearch.index import index_page
from pdfsearch.status import FileStatus, set_status


@pytest.fixture
def client(redis):
    # Swap the app's Redis client for the test database's client.
    app.dependency_overrides[get_redis] = lambda: redis
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_search(client, redis):
    index_page(redis, "b.pdf", 1, "Consensus with Raft")
    index_page(redis, "a.pdf", 4, "raft and paxos")
    index_page(redis, "a.pdf", 5, "nothing relevant")

    response = client.get("/search", params={"q": "RAFT!"})

    assert response.status_code == 200
    assert response.json() == {
        "query": "RAFT!",
        "results": [{"file": "a.pdf", "page": 4}, {"file": "b.pdf", "page": 1}],
    }


def test_search_without_matches(client):
    response = client.get("/search", params={"q": "nowhere"})
    assert response.status_code == 200
    assert response.json() == {"query": "nowhere", "results": []}


def test_search_too_short_is_bad_request(client):
    response = client.get("/search", params={"q": "C++"})
    assert response.status_code == 400
    assert "at least 3 characters" in response.json()["detail"]


def test_search_without_query_is_rejected(client):
    # Missing required parameter: FastAPI's validation answers 422.
    assert client.get("/search").status_code == 422


def test_status(client, redis):
    set_status(redis, "a.pdf", FileStatus.DONE, pages=3)
    set_status(redis, "b.pdf", FileStatus.DONE, pages=1)
    set_status(redis, "c.pdf", FileStatus.FAILED)

    response = client.get("/status")

    assert response.status_code == 200
    assert response.json() == {"processing": 0, "done": 2, "failed": 1}


def test_app_connects_using_redis_url(redis, redis_url, monkeypatch):
    # Without overrides: the lifespan creates the client from $REDIS_URL.
    monkeypatch.setenv("REDIS_URL", redis_url)
    set_status(redis, "a.pdf", FileStatus.PROCESSING)
    with TestClient(app) as client:  # "with" runs the lifespan
        assert client.get("/status").json() == {"processing": 1, "done": 0, "failed": 0}


def test_search_page(client):
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert '<form id="search-form">' in response.text


def test_livez_needs_nothing():
    # No Redis override, no lifespan: liveness must not depend on Redis.
    assert TestClient(app).get("/livez").json() == {"status": "ok"}


def test_readyz_when_redis_is_reachable(client):
    response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readyz_when_redis_is_down():
    unreachable = connect("redis://localhost:1/0")
    app.dependency_overrides[get_redis] = lambda: unreachable
    try:
        response = TestClient(app).get("/readyz")
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 503
    assert response.json()["status"] == "unavailable"
