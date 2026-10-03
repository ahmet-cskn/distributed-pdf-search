"""Watcher: uploads PDFs dropped into a folder to S3 (docs/DESIGN.md §3.1).

Run with: uv run pdfsearch-watch <folder> --bucket <bucket>
Each upload makes S3 send a job to the workers; the watcher creates no jobs.
"""

import argparse
import logging
import os
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import boto3

from pdfsearch.runtime import configure_logging, install_signal_handlers
from pdfsearch.scan import StabilityTracker, scan_folder
from pdfsearch.upload import UploadResult, list_bucket_keys, upload_files

log = logging.getLogger(__name__)


@dataclass
class Watcher:
    s3: Any
    bucket: str
    folder: Path
    poll_interval: float = 2
    max_concurrency: int = 8

    # Filenames known to be in the bucket: listed at startup or uploaded since.
    _in_bucket: set[str] = field(default_factory=set, init=False)
    _tracker: StabilityTracker = field(default_factory=StabilityTracker, init=False)
    _warned: set[str] = field(default_factory=set, init=False)

    def sync_with_bucket(self) -> None:
        """Learn which files are already uploaded, e.g. before a restart."""
        self._in_bucket = list_bucket_keys(self.s3, self.bucket)
        log.info("%d files already in s3://%s", len(self._in_bucket), self.bucket)

    def poll_once(self) -> UploadResult:
        """Scan the folder once and upload the PDFs that are ready and new."""
        scan = scan_folder(self.folder)

        for name in sorted(scan.unsupported - self._warned):
            log.warning("skipping %s: rename it to end in .pdf or .PDF", name)
            self._warned.add(name)

        # A file already in the bucket is skipped, even if its content changed:
        # updating indexed files is out of scope.
        ready = [n for n in self._tracker.ready(scan.files) if n not in self._in_bucket]
        result = upload_files(
            self.s3,
            self.bucket,
            [self.folder / name for name in ready],
            max_concurrency=self.max_concurrency,
        )
        # Failed uploads are not recorded, so the next poll retries them.
        self._in_bucket.update(result.uploaded)
        return result

    def run(self, stop: threading.Event) -> None:
        """Sync with the bucket, then poll until stop is set.

        Uploads in progress when stop is set are finished first.
        """
        self.sync_with_bucket()
        log.info("watching %s, polling every %gs", self.folder, self.poll_interval)
        while not stop.is_set():
            try:
                self.poll_once()
            except Exception:
                # E.g. the folder was removed or the network is down; try again.
                log.exception("polling %s failed", self.folder)
            stop.wait(self.poll_interval)
        log.info("watcher stopped")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pdfsearch-watch",
        description="Upload PDFs dropped into a folder to S3 for indexing.",
        epilog="AWS credentials and region come from the standard AWS settings "
        "(AWS_PROFILE, AWS_REGION, ...).",
    )
    parser.add_argument("folder", type=Path, help="folder to watch (subfolders are ignored)")
    parser.add_argument(
        "--bucket", default=os.environ.get("BUCKET"), help="S3 bucket (default: $BUCKET)"
    )
    parser.add_argument(
        "--poll-interval", type=float, default=2, help="seconds between scans (default: 2)"
    )
    args = parser.parse_args(argv)

    if not args.folder.is_dir():
        parser.error(f"not a directory: {args.folder}")
    if not args.bucket:
        parser.error("no bucket given: use --bucket or set BUCKET")

    configure_logging()
    watcher = Watcher(
        s3=boto3.client("s3"),
        bucket=args.bucket,
        folder=args.folder,
        poll_interval=args.poll_interval,
    )
    stop = threading.Event()
    install_signal_handlers(stop)
    try:
        watcher.run(stop)
    except Exception:
        # Startup sync failed: wrong bucket, credentials or network.
        log.exception("cannot list s3://%s", args.bucket)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
