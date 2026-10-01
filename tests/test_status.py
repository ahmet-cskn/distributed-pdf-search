import random
from concurrent.futures import ThreadPoolExecutor

from pdfsearch.status import (
    FileInfo,
    FileStatus,
    files_key,
    get_status,
    set_status,
    status_counts,
)

PROCESSING, DONE, FAILED = FileStatus.PROCESSING, FileStatus.DONE, FileStatus.FAILED


def counts(processing=0, done=0, failed=0):
    return {PROCESSING: processing, DONE: done, FAILED: failed}


def assert_consistent(redis):
    """Every file is in exactly one status set, the one its hash names."""
    members = {status: redis.smembers(files_key(status)) for status in FileStatus}
    files = set().union(*members.values())
    for file in files:
        in_sets = [status for status, names in members.items() if file in names]
        assert in_sets == [get_status(redis, file).status], file


def test_unknown_file_has_no_status(redis):
    assert get_status(redis, "a.pdf") is None
    assert status_counts(redis) == counts()


def test_processing_then_done(redis):
    set_status(redis, "a.pdf", PROCESSING)
    assert get_status(redis, "a.pdf") == FileInfo(PROCESSING, None)
    assert status_counts(redis) == counts(processing=1)

    set_status(redis, "a.pdf", DONE, pages=12)
    assert get_status(redis, "a.pdf") == FileInfo(DONE, 12)
    assert status_counts(redis) == counts(done=1)


def test_retry_after_failure(redis):
    set_status(redis, "a.pdf", FAILED)
    set_status(redis, "a.pdf", PROCESSING)
    assert status_counts(redis) == counts(processing=1)


def test_repeating_a_status_changes_nothing(redis):
    for _ in range(3):
        set_status(redis, "a.pdf", DONE, pages=5)
    assert get_status(redis, "a.pdf") == FileInfo(DONE, 5)
    assert status_counts(redis) == counts(done=1)


def test_page_count_is_kept_when_reprocessing(redis):
    set_status(redis, "a.pdf", DONE, pages=5)
    set_status(redis, "a.pdf", PROCESSING)
    assert get_status(redis, "a.pdf") == FileInfo(PROCESSING, 5)


def test_counts_across_files(redis):
    for i in range(5):
        set_status(redis, f"doc{i}.pdf", PROCESSING)
    for i in range(3):
        set_status(redis, f"doc{i}.pdf", DONE, pages=i + 1)
    set_status(redis, "doc3.pdf", FAILED)
    assert status_counts(redis) == counts(processing=1, done=3, failed=1)
    assert_consistent(redis)


def test_concurrent_status_changes_end_consistent(redis):
    # Many threads change the status of the same few files at the same time;
    # afterwards every file must be in exactly one set.
    #
    # Note: this cannot reliably reproduce the race that set_status's
    # transaction prevents. Redis usually executes a small pipelined batch from
    # one connection before serving the next, so even without MULTI/EXEC the
    # commands rarely interleave. The transaction turns "rarely" into "never".
    files = ["a.pdf", "b.pdf", "c.pdf"]

    def worker(seed):
        rng = random.Random(seed)
        for _ in range(200):
            set_status(redis, rng.choice(files), rng.choice(list(FileStatus)))

    with ThreadPoolExecutor(max_workers=8) as pool:
        for future in [pool.submit(worker, seed) for seed in range(8)]:
            future.result()

    assert_consistent(redis)
    assert sum(status_counts(redis).values()) == len(files)
