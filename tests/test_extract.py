import pymupdf
import pytest

from pdfsearch.extract import extract_pages
from pdfsearch.text import normalize


def test_pages_are_numbered_from_one(make_pdf):
    pdf = make_pdf(["first", "second", "third"])
    assert [number for number, _ in extract_pages(pdf)] == [1, 2, 3]


def test_text_round_trip(make_pdf):
    pages = ["Hello everyone!", "Distributed systems,\nlecture 2", "C++ and e.g. punctuation"]
    extracted = [text for _, text in extract_pages(make_pdf(pages))]
    # Extraction may add layout whitespace; after normalization it must match.
    assert [normalize(t) for t in extracted] == [normalize(t) for t in pages]


def test_blank_page_yields_empty_text(make_pdf):
    pdf = make_pdf(["text", "", "more text"])
    assert [normalize(text) for _, text in extract_pages(pdf)] == ["text", "", "more text"]


def test_reads_from_path(make_pdf, tmp_path):
    path = tmp_path / "lecture.pdf"
    path.write_bytes(make_pdf(["from a file"]))
    assert [(n, normalize(t)) for n, t in extract_pages(path)] == [(1, "from a file")]


def test_invalid_pdf_raises():
    with pytest.raises(pymupdf.FileDataError):
        list(extract_pages(b"this is not a pdf"))
