"""Indexing a whole PDF file: extraction, page indexing and status updates."""

import time
from pathlib import Path

from redis import Redis

from pdfsearch.extract import extract_pages
from pdfsearch.index import index_page
from pdfsearch.status import FileStatus, set_status


def index_file(
    redis: Redis,
    file: str,
    source: bytes | str | Path,
    *,
    timings: dict[str, float] | None = None,
) -> int:
    """Index every page of a PDF and return its page count.

    file is the name the PDF is indexed under; source is its content or path.
    If extraction or indexing fails, the exception propagates and the status
    stays "processing": the caller decides whether to retry or mark it failed.

    If timings is given, the seconds spent extracting text ("extract") and
    writing to Redis ("index") are added to it, e.g. for metrics.
    """
    spent = {"extract": 0.0, "index": 0.0}
    clock = time.perf_counter

    start = clock()
    set_status(redis, file, FileStatus.PROCESSING)
    spent["index"] += clock() - start

    pages = 0
    start = clock()
    # Extraction is lazy: each step of the loop extracts one page, so the
    # time until a page arrives is extraction, the time after it is indexing.
    for number, text in extract_pages(source):
        extracted = clock()
        spent["extract"] += extracted - start
        index_page(redis, file, number, text)
        start = clock()
        spent["index"] += start - extracted
        pages = number
    spent["extract"] += clock() - start  # closing the document

    start = clock()
    set_status(redis, file, FileStatus.DONE, pages=pages)
    spent["index"] += clock() - start

    if timings is not None:
        for phase, seconds in spent.items():
            timings[phase] = timings.get(phase, 0.0) + seconds
    return pages
