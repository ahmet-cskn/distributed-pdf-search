"""Worker: turns job messages from SQS into indexed PDFs (docs/DESIGN.md §3.3).

Run with: uv run pdfsearch-worker
Configured through environment variables, see main().
"""

import logging
import os
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any

import boto3
from prometheus_client import (
    Counter,
    Gauge,
    Histogram,
    disable_created_metrics,
    start_http_server,
)
from redis import Redis
from redis.exceptions import ConnectionError as RedisConnectionError

from pdfsearch.db import connect
from pdfsearch.heartbeat import VisibilityHeartbeat
from pdfsearch.ingest import index_file
from pdfsearch.jobs import InvalidJobMessage, S3Object, parse_job_message
from pdfsearch.runtime import configure_logging, install_signal_handlers
from pdfsearch.status import FileStatus, set_status

log = logging.getLogger(__name__)

# --- Metrics (scraped by Prometheus from /metrics, see main()) ---------------

JOBS = Counter(
    "pdfsearch_worker_jobs",
    "Processed job messages by outcome: done, skipped (no PDF, e.g. S3's test "
    "event), retry (failed, will be redelivered), failed (final attempt), "
    "invalid (not an S3 event), error (unexpected, e.g. delete failed).",
    ["outcome"],
)
FILES = Counter("pdfsearch_worker_files_indexed", "PDF files indexed.")
PAGES = Counter("pdfsearch_worker_pages_indexed", "PDF pages indexed.")
PHASE_SECONDS = Histogram(
    "pdfsearch_worker_phase_seconds",
    "Time per job spent in each phase: receive (only receives that returned "
    "a job), download (from S3), extract (PyMuPDF), index (Redis writes), "
    "delete (from SQS).",
    ["phase"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
)
BUSY = Gauge("pdfsearch_worker_busy", "1 while the worker is processing a job, else 0.")


@dataclass
class Worker:
    sqs: Any
    s3: Any
    redis: Redis
    queue_url: str
    # Must match maxReceiveCount of the queue's redrive policy (infra/sqs.tf):
    # on that attempt a failure is final, and SQS moves the message to the DLQ.
    max_receive_count: int = 3
    heartbeat_interval: float = 60
    visibility_timeout: int = 300
    # Long polling: how long one receive call waits for a message. Also the
    # longest a shutdown request can go unnoticed while the queue is idle.
    wait_time_seconds: int = 20
    # Pause after a failed receive (e.g. network down) before trying again.
    error_backoff_seconds: float = 5

    def run(self, stop: threading.Event) -> None:
        """Receive and process messages, one at a time, until stop is set.

        A message that is being processed when stop is set is finished first.
        """
        log.info("worker started, receiving from %s", self.queue_url)
        while not stop.is_set():
            start = time.perf_counter()
            try:
                response = self.sqs.receive_message(
                    QueueUrl=self.queue_url,
                    MaxNumberOfMessages=1,
                    WaitTimeSeconds=self.wait_time_seconds,
                    MessageSystemAttributeNames=["ApproximateReceiveCount"],
                )
            except Exception:
                log.exception("receiving from the queue failed; retrying")
                stop.wait(self.error_backoff_seconds)
                continue
            messages = response.get("Messages", [])
            if messages:
                # Empty receives are idle time (long polling), not job work.
                PHASE_SECONDS.labels("receive").observe(time.perf_counter() - start)

            for message in messages:
                if stop.is_set():
                    # Received while shutting down: hand it back right away
                    # instead of letting it wait out the visibility timeout.
                    self._release(message)
                    continue
                try:
                    self.process(message)
                except Exception:
                    # E.g. deleting the message failed. It will be redelivered.
                    JOBS.labels("error").inc()
                    log.exception("unexpected error processing message %s", message["MessageId"])
        log.info("worker stopped")

    def _release(self, message: dict) -> None:
        try:
            self.sqs.change_message_visibility(
                QueueUrl=self.queue_url,
                ReceiptHandle=message["ReceiptHandle"],
                VisibilityTimeout=0,
            )
        except Exception:
            log.warning("could not release message %s", message["MessageId"], exc_info=True)

    def process(self, message: dict) -> None:
        """Handle one received message.

        The message is deleted only after every object it names is indexed.
        On any failure it is left in the queue: SQS makes it visible again
        after the visibility timeout, and after max_receive_count attempts
        moves it to the dead-letter queue.

        The message must have been received with the ApproximateReceiveCount
        system attribute.
        """
        with BUSY.track_inprogress():
            JOBS.labels(self._process(message)).inc()

    def _process(self, message: dict) -> str:
        """Handle one message; return its outcome for the jobs metric."""
        receive_count = int(message.get("Attributes", {}).get("ApproximateReceiveCount", 1))
        final_attempt = receive_count >= self.max_receive_count

        try:
            objects = parse_job_message(message["Body"])
        except InvalidJobMessage:
            # Retrying cannot fix it; leaving it lets it end up in the DLQ
            # for inspection.
            log.exception("invalid job message %s", message["MessageId"])
            return "invalid"

        with VisibilityHeartbeat(
            self.sqs,
            self.queue_url,
            message["ReceiptHandle"],
            interval=self.heartbeat_interval,
            extend_to=self.visibility_timeout,
        ):
            for obj in objects:
                try:
                    self._index(obj)
                except Exception:
                    log.exception(
                        "failed to index %s (attempt %d of %d)",
                        obj.key,
                        receive_count,
                        self.max_receive_count,
                    )
                    if final_attempt:
                        self._mark_failed(obj.key)
                        return "failed"
                    return "retry"

        start = time.perf_counter()
        self.sqs.delete_message(QueueUrl=self.queue_url, ReceiptHandle=message["ReceiptHandle"])
        PHASE_SECONDS.labels("delete").observe(time.perf_counter() - start)
        return "done" if objects else "skipped"

    def _index(self, obj: S3Object) -> None:
        start = time.perf_counter()
        pdf = self.s3.get_object(Bucket=obj.bucket, Key=obj.key)["Body"].read()
        PHASE_SECONDS.labels("download").observe(time.perf_counter() - start)

        timings: dict[str, float] = {}
        pages = index_file(self.redis, obj.key, pdf, timings=timings)
        for phase, seconds in timings.items():
            PHASE_SECONDS.labels(phase).observe(seconds)
        FILES.inc()
        PAGES.inc(pages)
        log.info("indexed %s (%d pages)", obj.key, pages)

    def _mark_failed(self, file: str) -> None:
        try:
            set_status(self.redis, file, FileStatus.FAILED)
        except Exception:
            # E.g. Redis is down. The message still goes to the DLQ.
            log.exception("could not mark %s as failed", file)


def main() -> int:
    """Entry point of pdfsearch-worker.

    Environment variables:
        QUEUE_URL           SQS job queue (required)
        REDIS_URL           Redis to index into (default: see pdfsearch.db)
        AWS_DEFAULT_REGION, AWS_PROFILE or AWS access keys: standard AWS SDK settings
        MAX_RECEIVE_COUNT   must match the queue's redrive policy (default 3)
        VISIBILITY_TIMEOUT  seconds a message stays hidden per extension (default 300)
        HEARTBEAT_INTERVAL  seconds between extensions (default 60)
        METRICS_PORT        port of the Prometheus /metrics endpoint (default 9100)
        LOG_LEVEL           default INFO
    """
    configure_logging()

    queue_url = os.environ.get("QUEUE_URL")
    if not queue_url:
        log.error("QUEUE_URL is not set")
        return 2

    redis = connect()
    try:
        redis.ping()
    except RedisConnectionError:
        log.exception("cannot reach Redis")
        return 1

    worker = Worker(
        sqs=boto3.client("sqs"),
        s3=boto3.client("s3"),
        redis=redis,
        queue_url=queue_url,
        max_receive_count=int(os.environ.get("MAX_RECEIVE_COUNT", 3)),
        visibility_timeout=int(os.environ.get("VISIBILITY_TIMEOUT", 300)),
        heartbeat_interval=float(os.environ.get("HEARTBEAT_INTERVAL", 60)),
    )
    metrics_port = int(os.environ.get("METRICS_PORT", 9100))
    # Skip the extra *_created series per counter; nothing uses them.
    disable_created_metrics()
    start_http_server(metrics_port)
    log.info("serving metrics on port %d", metrics_port)

    stop = threading.Event()
    install_signal_handlers(stop)
    worker.run(stop)
    return 0


if __name__ == "__main__":
    sys.exit(main())
