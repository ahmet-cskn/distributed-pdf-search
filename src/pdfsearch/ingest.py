"""Indexing a whole PDF file: extraction, page indexing and status updates."""

from pathlib import Path

from redis import Redis

from pdfsearch.extract import extract_pages
from pdfsearch.index import index_page
from pdfsearch.status import FileStatus, set_status


def index_file(redis: Redis, file: str, source: bytes | str | Path) -> int:
    """Index every page of a PDF and return its page count.

    file is the name the PDF is indexed under; source is its content or path.
    If extraction or indexing fails, the exception propagates and the status
    stays "processing": the caller decides whether to retry or mark it failed.
    """
    set_status(redis, file, FileStatus.PROCESSING)
    pages = 0
    for number, text in extract_pages(source):
        index_page(redis, file, number, text)
        pages = number
    set_status(redis, file, FileStatus.DONE, pages=pages)
    return pages
