import json
import threading
import time

import pytest
from prometheus_client import REGISTRY, generate_latest

from pdfsearch.search import PageMatch, search
from pdfsearch.status import FileInfo, FileStatus, get_status, status_counts
from pdfsearch.worker import Worker, main


@pytest.fixture
def worker(sqs, s3, redis, queue_url):
    return Worker(sqs=sqs, s3=s3, redis=redis, queue_url=queue_url)


def receive(sqs, queue_url):
    """Receive the next message the way the worker loop does."""
    response = sqs.receive_message(
        QueueUrl=queue_url,
        MaxNumberOfMessages=1,
        MessageSystemAttributeNames=["ApproximateReceiveCount"],
    )
    [message] = response["Messages"]
    return message


def make_visible(sqs, queue_url, message):
    """Skip the visibility timeout, as if it had expired."""
    sqs.change_message_visibility(
        QueueUrl=queue_url, ReceiptHandle=message["ReceiptHandle"], VisibilityTimeout=0
    )


def queue_is_empty(sqs, queue_url):
    attributes = sqs.get_queue_attributes(
        QueueUrl=queue_url,
        AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"],
    )["Attributes"]
    return all(count == "0" for count in attributes.values())


def test_uploaded_pdf_becomes_searchable(worker, sqs, s3, redis, queue_url, bucket, make_pdf):
    s3.put_object(Bucket=bucket, Key="lecture1.pdf", Body=make_pdf(["Intro", "Raft consensus"]))

    worker.process(receive(sqs, queue_url))

    assert search(redis, "raft") == [PageMatch("lecture1.pdf", 2)]
    assert get_status(redis, "lecture1.pdf") == FileInfo(FileStatus.DONE, 2)
    assert queue_is_empty(sqs, queue_url)


def test_key_with_special_characters(worker, sqs, s3, redis, queue_url, bucket, make_pdf):
    # S3 sends this key URL-encoded ("lecture+1+%C3%BCbung.PDF").
    s3.put_object(Bucket=bucket, Key="lecture 1 übung.PDF", Body=make_pdf(["Paxos"]))

    worker.process(receive(sqs, queue_url))

    assert search(redis, "paxos") == [PageMatch("lecture 1 übung.PDF", 1)]


def test_test_event_is_deleted_without_indexing(worker, sqs, redis, queue_url):
    sqs.send_message(
        QueueUrl=queue_url,
        MessageBody=json.dumps({"Service": "Amazon S3", "Event": "s3:TestEvent"}),
    )

    worker.process(receive(sqs, queue_url))

    assert queue_is_empty(sqs, queue_url)
    assert redis.dbsize() == 0


def test_broken_pdf_is_retried(worker, sqs, s3, redis, queue_url, bucket):
    s3.put_object(Bucket=bucket, Key="broken.pdf", Body=b"not a pdf")
    message = receive(sqs, queue_url)

    worker.process(message)

    # Not deleted: once the visibility timeout expires, it comes back.
    assert get_status(redis, "broken.pdf").status == FileStatus.PROCESSING
    make_visible(sqs, queue_url, message)
    assert receive(sqs, queue_url)["Attributes"]["ApproximateReceiveCount"] == "2"


def test_broken_pdf_is_marked_failed_on_final_attempt(worker, sqs, s3, redis, queue_url, bucket):
    s3.put_object(Bucket=bucket, Key="broken.pdf", Body=b"not a pdf")

    for attempt in [1, 2, 3]:
        message = receive(sqs, queue_url)
        assert message["Attributes"]["ApproximateReceiveCount"] == str(attempt)
        worker.process(message)
        expected = FileStatus.FAILED if attempt == 3 else FileStatus.PROCESSING
        assert get_status(redis, "broken.pdf").status == expected
        make_visible(sqs, queue_url, message)


def test_missing_object_is_a_failure(worker, sqs, s3, redis, queue_url, bucket, make_pdf):
    s3.put_object(Bucket=bucket, Key="gone.pdf", Body=make_pdf(["text"]))
    s3.delete_object(Bucket=bucket, Key="gone.pdf")

    worker.process(receive(sqs, queue_url))

    assert not queue_is_empty(sqs, queue_url)
    assert search(redis, "text") == []


def test_invalid_message_is_left_for_the_dlq(worker, sqs, redis, queue_url):
    sqs.send_message(QueueUrl=queue_url, MessageBody="not json")

    worker.process(receive(sqs, queue_url))

    assert not queue_is_empty(sqs, queue_url)
    assert redis.dbsize() == 0


# --- The receive loop -------------------------------------------------------


def run_until(worker, condition, timeout=15):
    """Run the worker loop in a thread until condition() holds, then stop it."""
    stop = threading.Event()
    thread = threading.Thread(target=worker.run, args=(stop,))
    thread.start()
    try:
        deadline = time.monotonic() + timeout
        while not condition():
            assert time.monotonic() < deadline, "condition not reached in time"
            time.sleep(0.05)
    finally:
        stop.set()
        thread.join(timeout=10)
    assert not thread.is_alive(), "worker did not stop"


@pytest.fixture
def fast_worker(worker):
    worker.wait_time_seconds = 1
    return worker


def test_loop_indexes_every_upload(fast_worker, sqs, s3, redis, queue_url, bucket, make_pdf):
    for i in range(5):
        s3.put_object(Bucket=bucket, Key=f"doc{i}.pdf", Body=make_pdf([f"page of document {i}"]))

    run_until(fast_worker, lambda: status_counts(redis)[FileStatus.DONE] == 5)

    assert len(search(redis, "document")) == 5
    assert queue_is_empty(sqs, queue_url)


def test_broken_pdf_ends_up_in_dlq(fast_worker, sqs, s3, redis, queue_url, dlq_url, bucket):
    # Let failed attempts come back after 1 s instead of 5 minutes.
    sqs.set_queue_attributes(QueueUrl=queue_url, Attributes={"VisibilityTimeout": "1"})
    s3.put_object(Bucket=bucket, Key="broken.pdf", Body=b"not a pdf")

    def in_dlq():
        attributes = sqs.get_queue_attributes(
            QueueUrl=dlq_url, AttributeNames=["ApproximateNumberOfMessages"]
        )
        return attributes["Attributes"]["ApproximateNumberOfMessages"] == "1"

    run_until(fast_worker, in_dlq)

    assert get_status(redis, "broken.pdf").status == FileStatus.FAILED
    assert queue_is_empty(sqs, queue_url)


def test_stops_immediately_if_already_stopped(worker):
    stop = threading.Event()
    stop.set()
    worker.run(stop)  # returns without receiving


class StopAfterReceive:
    """Wraps an SQS client: the stop request arrives during a receive call."""

    def __init__(self, sqs, stop):
        self._sqs = sqs
        self._stop = stop

    def receive_message(self, **kwargs):
        response = self._sqs.receive_message(**kwargs)
        self._stop.set()
        return response

    def __getattr__(self, name):
        return getattr(self._sqs, name)


def test_message_received_during_shutdown_is_released(
    worker, sqs, s3, redis, queue_url, bucket, make_pdf
):
    s3.put_object(Bucket=bucket, Key="a.pdf", Body=make_pdf(["text"]))
    stop = threading.Event()
    worker.sqs = StopAfterReceive(sqs, stop)

    worker.run(stop)

    # Not processed, and immediately available to another worker.
    assert get_status(redis, "a.pdf") is None
    assert receive(sqs, queue_url)["Attributes"]["ApproximateReceiveCount"] == "2"


class FailingOnce:
    """Wraps an SQS client: the first receive call fails like a network error."""

    def __init__(self, sqs):
        self._sqs = sqs
        self.failed = False

    def receive_message(self, **kwargs):
        if not self.failed:
            self.failed = True
            raise ConnectionError("network unreachable")
        return self._sqs.receive_message(**kwargs)

    def __getattr__(self, name):
        return getattr(self._sqs, name)


def test_loop_survives_a_failed_receive(fast_worker, sqs, s3, redis, bucket, make_pdf):
    s3.put_object(Bucket=bucket, Key="a.pdf", Body=make_pdf(["text"]))
    fast_worker.sqs = FailingOnce(sqs)
    fast_worker.error_backoff_seconds = 0.1

    run_until(fast_worker, lambda: status_counts(redis)[FileStatus.DONE] == 1)

    assert fast_worker.sqs.failed


# --- Process setup ------------------------------------------------------------


def test_main_requires_queue_url(monkeypatch):
    monkeypatch.delenv("QUEUE_URL", raising=False)
    assert main() == 2


# --- Metrics ------------------------------------------------------------------


def sample(name, **labels):
    return REGISTRY.get_sample_value(name, labels) or 0.0


class MetricDeltas:
    """Metric values relative to when the object was created.

    Metrics live in a process-wide registry, so tests compare before/after.
    """

    def __init__(self):
        self._start = {}

    def __call__(self, name, **labels):
        key = (name, tuple(sorted(labels.items())))
        if key not in self._start:
            raise KeyError(f"take a baseline first: {key}")
        return sample(name, **labels) - self._start[key]

    def baseline(self, name, **labels):
        self._start[(name, tuple(sorted(labels.items())))] = sample(name, **labels)


PHASES = ["download", "extract", "index", "delete"]


def baseline_all():
    deltas = MetricDeltas()
    for outcome in ["done", "skipped", "retry", "failed", "invalid", "error"]:
        deltas.baseline("pdfsearch_worker_jobs_total", outcome=outcome)
    deltas.baseline("pdfsearch_worker_files_indexed_total")
    deltas.baseline("pdfsearch_worker_pages_indexed_total")
    for phase in [*PHASES, "receive"]:
        deltas.baseline("pdfsearch_worker_phase_seconds_count", phase=phase)
        deltas.baseline("pdfsearch_worker_phase_seconds_sum", phase=phase)
    return deltas


def test_metrics_for_an_indexed_file(worker, sqs, s3, queue_url, bucket, make_pdf):
    s3.put_object(Bucket=bucket, Key="a.pdf", Body=make_pdf(["one", "two", "three"]))
    delta = baseline_all()

    worker.process(receive(sqs, queue_url))

    assert delta("pdfsearch_worker_jobs_total", outcome="done") == 1
    assert delta("pdfsearch_worker_files_indexed_total") == 1
    assert delta("pdfsearch_worker_pages_indexed_total") == 3
    for phase in PHASES:
        assert delta("pdfsearch_worker_phase_seconds_count", phase=phase) == 1, phase
        assert delta("pdfsearch_worker_phase_seconds_sum", phase=phase) > 0, phase
    assert sample("pdfsearch_worker_busy") == 0  # back to idle


@pytest.mark.parametrize(
    ("body", "outcome"),
    [
        (json.dumps({"Service": "Amazon S3", "Event": "s3:TestEvent"}), "skipped"),
        ("not json", "invalid"),
    ],
)
def test_metrics_for_messages_without_pdfs(worker, sqs, queue_url, body, outcome):
    sqs.send_message(QueueUrl=queue_url, MessageBody=body)
    delta = baseline_all()

    worker.process(receive(sqs, queue_url))

    assert delta("pdfsearch_worker_jobs_total", outcome=outcome) == 1
    assert delta("pdfsearch_worker_files_indexed_total") == 0


def test_metrics_for_retries_and_final_failure(worker, sqs, s3, queue_url, bucket):
    s3.put_object(Bucket=bucket, Key="broken.pdf", Body=b"not a pdf")
    delta = baseline_all()

    for _ in range(3):
        message = receive(sqs, queue_url)
        worker.process(message)
        make_visible(sqs, queue_url, message)

    assert delta("pdfsearch_worker_jobs_total", outcome="retry") == 2
    assert delta("pdfsearch_worker_jobs_total", outcome="failed") == 1
    assert delta("pdfsearch_worker_files_indexed_total") == 0


def test_receive_is_timed_only_when_it_returns_a_job(fast_worker, sqs, s3, redis, bucket, make_pdf):
    delta = baseline_all()
    s3.put_object(Bucket=bucket, Key="a.pdf", Body=make_pdf(["text"]))

    run_until(fast_worker, lambda: status_counts(redis)[FileStatus.DONE] == 1)

    assert delta("pdfsearch_worker_phase_seconds_count", phase="receive") == 1


def test_metrics_are_exported():
    exported = generate_latest(REGISTRY).decode()
    for name in [
        "pdfsearch_worker_jobs_total",
        "pdfsearch_worker_files_indexed_total",
        "pdfsearch_worker_pages_indexed_total",
        "pdfsearch_worker_phase_seconds_bucket",
        "pdfsearch_worker_busy",
    ]:
        assert name in exported, name
