"""Extracting the text of each page of a PDF."""

from collections.abc import Iterator
from pathlib import Path

import pymupdf


def extract_pages(source: bytes | str | Path) -> Iterator[tuple[int, str]]:
    """Yield (page number, raw text) for every page of a PDF, 1-based.

    source is either the PDF's content (e.g. downloaded from S3) or a path.
    Pages are yielded one at a time, so a large PDF is never held in memory
    as text all at once. Pages without a text layer (e.g. scans) yield "".

    Raises:
        pymupdf.FileDataError: if source is not a readable PDF.
    """
    if isinstance(source, bytes):
        doc = pymupdf.open(stream=source, filetype="pdf")
    else:
        doc = pymupdf.open(source, filetype="pdf")

    with doc:
        for index, page in enumerate(doc):
            yield index + 1, page.get_text()
