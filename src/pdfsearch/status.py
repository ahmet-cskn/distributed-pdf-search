"""Per-file processing status, for progress tracking.

Key layout (see docs/DESIGN.md §4.3):
    file:<file>      Hash  status, and pages once known
    files:<status>   Set   filenames currently in that status

Files waiting in the queue have no status yet; they show up as queue length.
"""

from dataclasses import dataclass
from enum import StrEnum

from redis import Redis


class FileStatus(StrEnum):
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"


@dataclass(frozen=True)
class FileInfo:
    status: FileStatus
    pages: int | None


def file_key(file: str) -> str:
    return f"file:{file}"


def files_key(status: FileStatus) -> str:
    return f"files:{status}"


def set_status(redis: Redis, file: str, status: FileStatus, pages: int | None = None) -> None:
    """Record a file's status, and its page count if given.

    The hash and the status sets are updated in one MULTI/EXEC transaction,
    so no client can ever observe the file in two sets, or in a set that
    disagrees with its hash, even while other workers change it concurrently.
    """
    fields: dict[str, str | int] = {"status": status}
    if pages is not None:
        fields["pages"] = pages

    pipe = redis.pipeline(transaction=True)
    pipe.hset(file_key(file), mapping=fields)
    # Remove from every other set instead of reading the current status first:
    # no read means no race between reading and writing, and repeating the
    # call is harmless.
    for other in FileStatus:
        if other != status:
            pipe.srem(files_key(other), file)
    pipe.sadd(files_key(status), file)
    pipe.execute()


def get_status(redis: Redis, file: str) -> FileInfo | None:
    """Return a file's status, or None if no worker has picked it up yet."""
    fields = redis.hgetall(file_key(file))
    if not fields:
        return None
    pages = fields.get("pages")
    return FileInfo(FileStatus(fields["status"]), int(pages) if pages is not None else None)


def status_counts(redis: Redis) -> dict[FileStatus, int]:
    """Number of files in each status."""
    pipe = redis.pipeline(transaction=False)
    for status in FileStatus:
        pipe.scard(files_key(status))
    return dict(zip(FileStatus, pipe.execute(), strict=True))
