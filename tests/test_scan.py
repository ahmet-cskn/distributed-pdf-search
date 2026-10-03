import os

import pytest

from pdfsearch.scan import FileState, StabilityTracker, scan_folder


def write(path, data=b"%PDF-1.7 content"):
    path.write_bytes(data)
    return path


def test_finds_pdfs_with_supported_suffixes(tmp_path):
    write(tmp_path / "lecture1.pdf")
    write(tmp_path / "LECTURE2.PDF")
    write(tmp_path / "lecture 3 übung.pdf")

    assert sorted(scan_folder(tmp_path).files) == [
        "LECTURE2.PDF",
        "lecture 3 übung.pdf",
        "lecture1.pdf",
    ]


def test_reports_size_and_modification_time(tmp_path):
    path = write(tmp_path / "a.pdf", b"12345")
    os.utime(path, ns=(1_000_000_000, 2_000_000_000))

    assert scan_folder(tmp_path).files == {"a.pdf": FileState(5, 2_000_000_000)}


def test_mixed_case_suffix_is_reported_but_not_uploadable(tmp_path):
    write(tmp_path / "slides.Pdf")

    scan = scan_folder(tmp_path)

    assert scan.files == {}
    assert scan.unsupported == {"slides.Pdf"}


@pytest.mark.parametrize(
    "name",
    [
        "notes.txt",
        "pdf",  # no suffix
        "archive.pdf.zip",
        ".hidden.pdf",
        "._lecture1.pdf",  # macOS metadata file
        ".DS_Store",
    ],
)
def test_ignores_other_files(tmp_path, name):
    write(tmp_path / name)
    scan = scan_folder(tmp_path)
    assert scan.files == {}
    assert scan.unsupported == set()


def test_ignores_subfolders_and_their_contents(tmp_path):
    (tmp_path / "folder.pdf").mkdir()  # a directory that looks like a PDF
    (tmp_path / "sub").mkdir()
    write(tmp_path / "sub" / "nested.pdf")

    assert scan_folder(tmp_path).files == {}


def test_empty_folder(tmp_path):
    assert scan_folder(tmp_path).files == {}


def test_missing_folder_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        scan_folder(tmp_path / "missing")


A = FileState(size=100, mtime_ns=1)


def test_file_is_ready_once_unchanged():
    tracker = StabilityTracker()
    assert tracker.ready({"a.pdf": A}) == []  # first sighting
    assert tracker.ready({"a.pdf": A}) == ["a.pdf"]


def test_growing_file_is_not_ready():
    tracker = StabilityTracker()
    tracker.ready({"a.pdf": FileState(100, 1)})
    assert tracker.ready({"a.pdf": FileState(200, 2)}) == []  # still being copied
    assert tracker.ready({"a.pdf": FileState(200, 2)}) == ["a.pdf"]


def test_rewritten_file_with_same_size_is_not_ready():
    # Some tools allocate the full size first and fill it in afterwards;
    # the modification time still changes.
    tracker = StabilityTracker()
    tracker.ready({"a.pdf": FileState(100, 1)})
    assert tracker.ready({"a.pdf": FileState(100, 2)}) == []


def test_empty_file_is_never_ready():
    tracker = StabilityTracker()
    tracker.ready({"a.pdf": FileState(0, 1)})
    assert tracker.ready({"a.pdf": FileState(0, 1)}) == []


def test_files_are_tracked_independently():
    tracker = StabilityTracker()
    tracker.ready({"a.pdf": A})
    assert tracker.ready({"a.pdf": A, "b.pdf": A}) == ["a.pdf"]
    assert tracker.ready({"a.pdf": A, "b.pdf": A}) == ["a.pdf", "b.pdf"]


def test_deleted_file_is_forgotten():
    tracker = StabilityTracker()
    tracker.ready({"a.pdf": A})
    tracker.ready({})  # deleted
    assert tracker.ready({"a.pdf": A}) == []  # reappeared: first sighting again


def test_with_real_files(tmp_path):
    tracker = StabilityTracker()
    path = write(tmp_path / "a.pdf", b"part 1")
    assert tracker.ready(scan_folder(tmp_path).files) == []

    with path.open("ab") as f:  # the copy continues
        f.write(b" part 2")
    assert tracker.ready(scan_folder(tmp_path).files) == []

    assert tracker.ready(scan_folder(tmp_path).files) == ["a.pdf"]
