import pymupdf
import pytest

from pdfsearch.ingest import index_file
from pdfsearch.local_index import main
from pdfsearch.search import PageMatch, search
from pdfsearch.status import FileInfo, FileStatus, get_status, status_counts


def test_index_file_makes_pages_searchable(redis, make_pdf):
    pdf = make_pdf(["Intro to distributed systems", "", "Consensus and Raft"])
    assert index_file(redis, "lecture1.pdf", pdf) == 3
    assert search(redis, "raft") == [PageMatch("lecture1.pdf", 3)]
    assert get_status(redis, "lecture1.pdf") == FileInfo(FileStatus.DONE, 3)


def test_index_file_leaves_failure_handling_to_the_caller(redis):
    with pytest.raises(pymupdf.FileDataError):
        index_file(redis, "broken.pdf", b"not a pdf")
    assert get_status(redis, "broken.pdf").status == FileStatus.PROCESSING


def test_cli_indexes_a_folder(redis, redis_url, make_pdf, tmp_path, capsys):
    (tmp_path / "a.pdf").write_bytes(make_pdf(["alpha page", "shared words"]))
    (tmp_path / "B.PDF").write_bytes(make_pdf(["shared words again"]))
    (tmp_path / "broken.pdf").write_bytes(b"not a pdf")
    (tmp_path / "notes.txt").write_text("shared words, but not a pdf")

    exit_code = main([str(tmp_path), "--redis-url", redis_url])

    assert exit_code == 1  # one file failed
    assert search(redis, "shared words") == [PageMatch("B.PDF", 1), PageMatch("a.pdf", 2)]
    assert status_counts(redis) == {
        FileStatus.PROCESSING: 0,
        FileStatus.DONE: 2,
        FileStatus.FAILED: 1,
    }
    out, err = capsys.readouterr()
    assert "Indexed 2 of 3 files, 3 pages" in out
    assert "FAILED broken.pdf" in err


def test_cli_succeeds_when_all_files_index(redis, redis_url, make_pdf, tmp_path):
    (tmp_path / "a.pdf").write_bytes(make_pdf(["hello"]))
    assert main([str(tmp_path), "--redis-url", redis_url]) == 0


def test_cli_rejects_missing_folder(tmp_path):
    with pytest.raises(SystemExit) as exc:
        main([str(tmp_path / "missing")])
    assert exc.value.code == 2


def test_cli_reports_unreachable_redis(tmp_path, capsys):
    assert main([str(tmp_path), "--redis-url", "redis://localhost:1/0"]) == 2
    assert "Cannot reach Redis" in capsys.readouterr().err


def test_index_file_reports_time_per_phase(redis, make_pdf):
    timings = {"extract": 1.0}  # existing values are added to, not replaced
    index_file(redis, "a.pdf", make_pdf(["one", "two"]), timings=timings)

    assert set(timings) == {"extract", "index"}
    assert timings["extract"] > 1.0
    assert timings["index"] > 0
