"""pdfsearch-index: index every PDF in a local folder, in a single process.

A development tool, and the single-process baseline that the distributed
pipeline's throughput is compared against.
"""

import argparse
import sys
import time
from pathlib import Path

from redis.exceptions import ConnectionError

from pdfsearch.db import DEFAULT_REDIS_URL, connect
from pdfsearch.ingest import index_file
from pdfsearch.status import FileStatus, set_status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pdfsearch-index",
        description="Index every PDF in a folder into Redis, in a single process.",
    )
    parser.add_argument("folder", type=Path, help="folder containing the PDFs")
    parser.add_argument(
        "--redis-url", help=f"Redis to write to (default: $REDIS_URL or {DEFAULT_REDIS_URL})"
    )
    args = parser.parse_args(argv)

    if not args.folder.is_dir():
        parser.error(f"not a directory: {args.folder}")
    pdfs = sorted(p for p in args.folder.iterdir() if p.is_file() and p.suffix.lower() == ".pdf")

    redis = connect(args.redis_url)
    try:
        redis.ping()
    except ConnectionError as e:
        print(f"Cannot reach Redis: {e}", file=sys.stderr)
        return 2

    start = time.perf_counter()
    total_pages = failed = 0
    for path in pdfs:
        try:
            pages = index_file(redis, path.name, path)
        except Exception as e:
            # A broken PDF must not stop the rest of the batch.
            set_status(redis, path.name, FileStatus.FAILED)
            print(f"FAILED {path.name}: {e}", file=sys.stderr)
            failed += 1
            continue
        total_pages += pages
        print(f"{path.name}: {pages} pages")
    elapsed = time.perf_counter() - start

    rate = total_pages / elapsed if elapsed > 0 else 0
    print(
        f"Indexed {len(pdfs) - failed} of {len(pdfs)} files, "
        f"{total_pages} pages in {elapsed:.2f}s ({rate:.0f} pages/s)"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
