"""HTTP query service.

Run locally with: uv run uvicorn pdfsearch.api:app --reload
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from redis import Redis
from redis.exceptions import RedisError

from pdfsearch.db import connect
from pdfsearch.search import QueryTooShortError, search
from pdfsearch.status import status_counts

SEARCH_PAGE = Path(__file__).parent / "static" / "index.html"


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
    try:
        matches = search(redis, q)
    except QueryTooShortError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
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
