"""HTTP query service.

Run locally with: uv run uvicorn pdfsearch.api:app --reload
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from pydantic import BaseModel
from redis import Redis

from pdfsearch.db import connect
from pdfsearch.search import QueryTooShortError, search
from pdfsearch.status import status_counts


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
