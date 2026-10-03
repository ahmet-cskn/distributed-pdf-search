import logging
import threading
import time

import pytest

from pdfsearch.upload import list_bucket_keys
from pdfsearch.watcher import Watcher, main


@pytest.fixture
def folder(tmp_path):
    path = tmp_path / "inbox"
    path.mkdir()
    return path


@pytest.fixture
def watcher(s3, bucket, folder):
    w = Watcher(s3=s3, bucket=bucket, folder=folder)
    w.sync_with_bucket()
    return w


def test_file_is_uploaded_once_stable(watcher, s3, bucket, folder):
    (folder / "lecture1.pdf").write_bytes(b"%PDF content")

    assert watcher.poll_once().uploaded == []  # first sighting
    assert watcher.poll_once().uploaded == ["lecture1.pdf"]
    assert list_bucket_keys(s3, bucket) == {"lecture1.pdf"}


def test_uploaded_file_is_not_uploaded_again(watcher, folder):
    (folder / "lecture1.pdf").write_bytes(b"%PDF content")
    watcher.poll_once()
    watcher.poll_once()

    assert watcher.poll_once().uploaded == []


def test_files_already_in_bucket_are_skipped_after_restart(s3, bucket, folder):
    s3.put_object(Bucket=bucket, Key="old.pdf", Body=b"uploaded before the restart")
    (folder / "old.pdf").write_bytes(b"%PDF content")
    (folder / "new.pdf").write_bytes(b"%PDF content")

    restarted = Watcher(s3=s3, bucket=bucket, folder=folder)
    restarted.sync_with_bucket()
    restarted.poll_once()

    assert restarted.poll_once().uploaded == ["new.pdf"]
    body = s3.get_object(Bucket=bucket, Key="old.pdf")["Body"].read()
    assert body == b"uploaded before the restart"


def test_unsupported_suffix_is_warned_about_once(watcher, folder, caplog):
    (folder / "slides.Pdf").write_bytes(b"%PDF content")

    with caplog.at_level(logging.WARNING, logger="pdfsearch.watcher"):
        for _ in range(3):
            assert watcher.poll_once().uploaded == []

    warnings = [r.message for r in caplog.records if "slides.Pdf" in r.message]
    assert warnings == ["skipping slides.Pdf: rename it to end in .pdf or .PDF"]


class FailFirstUpload:
    """Wraps an S3 client: the first upload fails like a network error."""

    def __init__(self, s3):
        self._s3 = s3
        self.failed = False

    def upload_file(self, **kwargs):
        if not self.failed:
            self.failed = True
            raise ConnectionError("network unreachable")
        return self._s3.upload_file(**kwargs)

    def __getattr__(self, name):
        return getattr(self._s3, name)


def test_failed_upload_is_retried_on_next_poll(watcher, s3, folder):
    watcher.s3 = FailFirstUpload(s3)
    (folder / "lecture1.pdf").write_bytes(b"%PDF content")
    watcher.poll_once()

    assert list(watcher.poll_once().failed) == ["lecture1.pdf"]
    assert watcher.poll_once().uploaded == ["lecture1.pdf"]


def test_run_uploads_dropped_files_and_stops(s3, bucket, folder):
    watcher = Watcher(s3=s3, bucket=bucket, folder=folder, poll_interval=0.05)
    stop = threading.Event()
    thread = threading.Thread(target=watcher.run, args=(stop,))
    thread.start()
    try:
        for i in range(3):
            (folder / f"doc{i}.pdf").write_bytes(b"%PDF content")
        deadline = time.monotonic() + 10
        while len(list_bucket_keys(s3, bucket)) < 3:
            assert time.monotonic() < deadline, "files were not uploaded"
            time.sleep(0.05)
    finally:
        stop.set()
        thread.join(timeout=5)
    assert not thread.is_alive()


def test_run_survives_a_failed_poll(s3, bucket, folder):
    watcher = Watcher(s3=s3, bucket=bucket, folder=folder, poll_interval=0.05)
    stop = threading.Event()
    thread = threading.Thread(target=watcher.run, args=(stop,))
    folder.rmdir()  # polling fails while the folder is missing
    thread.start()
    try:
        time.sleep(0.2)
        folder.mkdir()
        (folder / "a.pdf").write_bytes(b"%PDF content")
        deadline = time.monotonic() + 10
        while not list_bucket_keys(s3, bucket):
            assert time.monotonic() < deadline, "watcher did not recover"
            time.sleep(0.05)
    finally:
        stop.set()
        thread.join(timeout=5)


def test_main_rejects_missing_folder(tmp_path):
    with pytest.raises(SystemExit) as exc:
        main([str(tmp_path / "missing"), "--bucket", "b"])
    assert exc.value.code == 2


def test_main_requires_a_bucket(folder, monkeypatch):
    monkeypatch.delenv("BUCKET", raising=False)
    with pytest.raises(SystemExit) as exc:
        main([str(folder)])
    assert exc.value.code == 2


def test_main_fails_when_bucket_cannot_be_listed(aws, folder, preserve_signal_handlers):
    assert main([str(folder), "--bucket", "no-such-bucket-pdfsearch"]) == 1
