"""HTTP query service.

Run locally with: uv run uvicorn pdfsearch.api:app --reload
"""

import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Histogram,
    disable_created_metrics,
    generate_latest,
)
from pydantic import BaseModel
from redis import Redis
from redis.exceptions import RedisError

from pdfsearch.db import connect
from pdfsearch.search import QueryTooShortError, SearchStats, search
from pdfsearch.status import status_counts

SEARCH_PAGE = Path(__file__).parent / "static" / "index.html"

# --- Metrics (served at /metrics) --------------------------------------------

# Skip the extra *_created series per counter; nothing uses them.
disable_created_metrics()

REQUESTS = Counter(
    "pdfsearch_api_requests",
    "HTTP requests by route template (not the raw URL, to keep the number of "
    "series small) and status code.",
    ["route", "status"],
)
REQUEST_SECONDS = Histogram(
    "pdfsearch_api_request_seconds",
    "HTTP request duration by route template.",
    ["route"],
    buckets=(0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5),
)
COUNT_BUCKETS = (0, 1, 10, 100, 1_000, 10_000, 100_000)
SEARCH_CANDIDATES = Histogram(
    "pdfsearch_search_candidates",
    "Pages returned by the trigram filter per search.",
    buckets=COUNT_BUCKETS,
)
SEARCH_MATCHES = Histogram(
    "pdfsearch_search_matches",
    "Pages that really contain the query, per search.",
    buckets=COUNT_BUCKETS,
)
SEARCH_PHASE_SECONDS = Histogram(
    "pdfsearch_search_phase_seconds",
    "Search time per phase: intersect (SINTER) and verify (MGET + substring check).",
    ["phase"],
    buckets=(0.0005, 0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1),
)


class SearchResult(BaseModel):
    file: str
    page: int


class SearchResponse(BaseModel):
    query: str
    results: list[SearchResult]


class StatusResponse(BaseModel):
    processing: int
    done: int
    failed: int


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # One client for the whole process; it keeps a pool of connections that
    # concurrent requests share.
    app.state.redis = connect()
    yield
    app.state.redis.close()


app = FastAPI(title="pdfsearch", lifespan=lifespan)


@app.middleware("http")
async def record_request_metrics(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    start = time.perf_counter()
    status = 500  # if the handler raises, the client gets a 500
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        # Routing fills in the matched route, e.g. "/search" for "/search?q=raft".
        route = request.scope.get("route")
        template = getattr(route, "path", "unmatched")
        REQUESTS.labels(template, str(status)).inc()
        REQUEST_SECONDS.labels(template).observe(time.perf_counter() - start)


def get_redis(request: Request) -> Redis:
    return request.app.state.redis


RedisDep = Annotated[Redis, Depends(get_redis)]


# Endpoints are plain "def", not "async def": the Redis client is blocking,
# so FastAPI runs them in a thread pool instead of on the event loop.
@app.get("/search")
def search_endpoint(
    redis: RedisDep,
    q: Annotated[str, Query(description="Text to search for (at least 3 characters)")],
) -> SearchResponse:
    stats = SearchStats()
    try:
        matches = search(redis, q, stats=stats)
    except QueryTooShortError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    SEARCH_CANDIDATES.observe(stats.candidates)
    SEARCH_MATCHES.observe(stats.matches)
    SEARCH_PHASE_SECONDS.labels("intersect").observe(stats.intersect_seconds)
    if stats.candidates:
        SEARCH_PHASE_SECONDS.labels("verify").observe(stats.verify_seconds)
    return SearchResponse(
        query=q, results=[SearchResult(file=m.file, page=m.page) for m in matches]
    )


@app.get("/status")
def status_endpoint(redis: RedisDep) -> StatusResponse:
    counts = status_counts(redis)
    return StatusResponse(**{status.value: count for status, count in counts.items()})


@app.get("/", include_in_schema=False)
def search_page() -> FileResponse:
    return FileResponse(SEARCH_PAGE)


# Health checks for Kubernetes probes. They are separate on purpose:
# - liveness: "is this process working at all?" Failing it makes Kubernetes
#   restart the container, which would not help if only Redis is down.
# - readiness: "can it serve requests right now?" Failing it only stops
#   traffic from being sent to this pod until it passes again.
@app.get("/livez", include_in_schema=False)
def livez() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/readyz", include_in_schema=False, response_model=None)
def readyz(redis: RedisDep) -> dict[str, str] | JSONResponse:
    try:
        redis.ping()
    except RedisError as e:
        return JSONResponse(status_code=503, content={"status": "unavailable", "reason": str(e)})
    return {"status": "ok"}


@app.get("/metrics", include_in_schema=False)
def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
