"""Uploading PDFs from the watched folder to S3."""

import logging
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def list_bucket_keys(s3: Any, bucket: str) -> set[str]:
    """Return every object key in the bucket.

    ListObjectsV2 returns at most 1000 keys per call; the paginator keeps
    calling until all pages are read.
    """
    keys: set[str] = set()
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket):
        keys.update(obj["Key"] for obj in page.get("Contents", []))
    return keys


@dataclass
class UploadResult:
    uploaded: list[str] = field(default_factory=list)
    failed: dict[str, Exception] = field(default_factory=dict)


def upload_files(
    s3: Any, bucket: str, paths: Iterable[Path], *, max_concurrency: int = 8
) -> UploadResult:
    """Upload files concurrently, each under its filename as the object key.

    upload_file switches to a multipart upload for large files and uploads
    their parts in parallel; the thread pool here additionally uploads
    several files at once, which is what speeds up many small files.
    A failed upload does not stop the others.
    """
    paths = list(paths)
    result = UploadResult()
    if not paths:
        return result

    def upload(path: Path) -> None:
        s3.upload_file(
            Filename=str(path),
            Bucket=bucket,
            Key=path.name,
            ExtraArgs={"ContentType": "application/pdf"},
        )

    with ThreadPoolExecutor(max_workers=max_concurrency, thread_name_prefix="upload") as pool:
        futures = {path.name: pool.submit(upload, path) for path in paths}
        for name, future in futures.items():
            try:
                future.result()
            except Exception as e:
                log.warning("upload of %s failed: %s", name, e)
                result.failed[name] = e
            else:
                log.info("uploaded %s", name)
                result.uploaded.append(name)
    return result
