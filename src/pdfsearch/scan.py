"""Finding PDFs in the watched folder that are completely written.

The watcher polls the folder (docs/DESIGN.md §3.1). A file that is still
being copied already shows up in the folder, so a file only counts as ready
once its size and modification time are unchanged between two scans.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

# Must match the suffix filters of the S3 event notification
# (infra/events.tf): an object with another suffix would never become a job.
PDF_SUFFIXES = (".pdf", ".PDF")


class FileState(NamedTuple):
    size: int
    mtime_ns: int


@dataclass
class FolderScan:
    # Uploadable PDFs: filename -> current state.
    files: dict[str, FileState] = field(default_factory=dict)
    # PDFs whose suffix is spelled differently, e.g. "slides.Pdf". They are
    # not uploaded because the S3 notification would ignore them.
    unsupported: set[str] = field(default_factory=set)


def scan_folder(folder: Path) -> FolderScan:
    """List the PDFs directly inside folder (subfolders are not searched).

    Hidden files are skipped, e.g. "._lecture.pdf", which macOS creates next
    to files on some drives and which only looks like a PDF.
    """
    scan = FolderScan()
    with os.scandir(folder) as entries:
        for entry in entries:
            name = entry.name
            if name.startswith(".") or not name.lower().endswith(".pdf"):
                continue
            try:
                if not entry.is_file():
                    continue
                stat = entry.stat()
            except FileNotFoundError:
                continue  # deleted between listing and stat
            if not name.endswith(PDF_SUFFIXES):
                scan.unsupported.add(name)
                continue
            scan.files[name] = FileState(stat.st_size, stat.st_mtime_ns)
    return scan


class StabilityTracker:
    """Tells which files did not change since the previous scan."""

    def __init__(self) -> None:
        self._previous: dict[str, FileState] = {}

    def ready(self, files: dict[str, FileState]) -> list[str]:
        """Return, sorted, the files that are unchanged since the last call.

        A file is never ready on the scan where it first appears. Empty files
        are never ready: a copy that has just started is typically empty.
        """
        ready = [
            name
            for name, state in files.items()
            if state.size > 0 and self._previous.get(name) == state
        ]
        self._previous = dict(files)
        return sorted(ready)
