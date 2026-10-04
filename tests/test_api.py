import pytest
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

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


# --- Metrics ------------------------------------------------------------------


def value(name, **labels):
    return REGISTRY.get_sample_value(name, labels) or 0.0


def test_requests_are_counted_by_route_template(client, redis):
    before = value("pdfsearch_api_requests_total", route="/search", status="200")
    before_400 = value("pdfsearch_api_requests_total", route="/search", status="400")

    client.get("/search", params={"q": "raft"})
    client.get("/search", params={"q": "paxos"})
    client.get("/search", params={"q": "ab"})

    # Two different query strings, one series: the label is the route, not the URL.
    assert value("pdfsearch_api_requests_total", route="/search", status="200") == before + 2
    assert value("pdfsearch_api_requests_total", route="/search", status="400") == before_400 + 1


def test_unknown_paths_share_one_label(client):
    before = value("pdfsearch_api_requests_total", route="unmatched", status="404")
    client.get("/no-such-page")
    client.get("/another-missing-page")
    assert value("pdfsearch_api_requests_total", route="unmatched", status="404") == before + 2


def test_request_duration_is_recorded(client):
    before = value("pdfsearch_api_request_seconds_count", route="/status")
    client.get("/status")
    assert value("pdfsearch_api_request_seconds_count", route="/status") == before + 1


class BrokenRedis:
    def sinter(self, keys):
        raise RuntimeError("unexpected failure")


def test_server_errors_are_counted():
    before = value("pdfsearch_api_requests_total", route="/search", status="500")
    app.dependency_overrides[get_redis] = lambda: BrokenRedis()
    try:
        response = TestClient(app, raise_server_exceptions=False).get(
            "/search", params={"q": "raft"}
        )
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 500
    assert value("pdfsearch_api_requests_total", route="/search", status="500") == before + 1


def test_search_candidates_and_matches(client, redis):
    # "abcde": two candidates (both pages have abc, bcd, cde), one real match.
    index_page(redis, "a.pdf", 1, "abcd xcde")
    index_page(redis, "b.pdf", 1, "abcde")
    candidates = value("pdfsearch_search_candidates_sum")
    matches = value("pdfsearch_search_matches_sum")
    verify = value("pdfsearch_search_phase_seconds_count", phase="verify")

    client.get("/search", params={"q": "abcde"})

    assert value("pdfsearch_search_candidates_sum") == candidates + 2
    assert value("pdfsearch_search_matches_sum") == matches + 1
    assert value("pdfsearch_search_phase_seconds_count", phase="verify") == verify + 1


def test_metrics_endpoint(client):
    client.get("/status")
    response = client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    for name in [
        "pdfsearch_api_requests_total",
        "pdfsearch_api_request_seconds_bucket",
        "pdfsearch_search_candidates_bucket",
        "pdfsearch_search_phase_seconds_bucket",
    ]:
        assert name in response.text, name
    assert "_created" not in response.text
