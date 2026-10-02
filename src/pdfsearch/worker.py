"""Worker: turns job messages from SQS into indexed PDFs (docs/DESIGN.md §3.3).

Run with: uv run pdfsearch-worker
Configured through environment variables, see main().
"""

import logging
import os
import signal
import sys
import threading
from dataclasses import dataclass
from typing import Any

import boto3
from redis import Redis
from redis.exceptions import ConnectionError as RedisConnectionError

from pdfsearch.db import connect
from pdfsearch.heartbeat import VisibilityHeartbeat
from pdfsearch.ingest import index_file
from pdfsearch.jobs import InvalidJobMessage, S3Object, parse_job_message
from pdfsearch.status import FileStatus, set_status

log = logging.getLogger(__name__)


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

            for message in response.get("Messages", []):
                if stop.is_set():
                    # Received while shutting down: hand it back right away
                    # instead of letting it wait out the visibility timeout.
                    self._release(message)
                    continue
                try:
                    self.process(message)
                except Exception:
                    # E.g. deleting the message failed. It will be redelivered.
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
        receive_count = int(message.get("Attributes", {}).get("ApproximateReceiveCount", 1))
        final_attempt = receive_count >= self.max_receive_count

        try:
            objects = parse_job_message(message["Body"])
        except InvalidJobMessage:
            # Retrying cannot fix it; leaving it lets it end up in the DLQ
            # for inspection.
            log.exception("invalid job message %s", message["MessageId"])
            return

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
                    return

        self.sqs.delete_message(QueueUrl=self.queue_url, ReceiptHandle=message["ReceiptHandle"])

    def _index(self, obj: S3Object) -> None:
        pdf = self.s3.get_object(Bucket=obj.bucket, Key=obj.key)["Body"].read()
        pages = index_file(self.redis, obj.key, pdf)
        log.info("indexed %s (%d pages)", obj.key, pages)

    def _mark_failed(self, file: str) -> None:
        try:
            set_status(self.redis, file, FileStatus.FAILED)
        except Exception:
            # E.g. Redis is down. The message still goes to the DLQ.
            log.exception("could not mark %s as failed", file)


def install_signal_handlers(stop: threading.Event) -> None:
    """SIGTERM (sent by Kubernetes) or Ctrl+C: finish the current job, then exit.

    A second signal exits immediately; the job being processed is then not
    deleted and will be redelivered.
    """

    def handle(signum: int, frame: Any) -> None:
        if stop.is_set():
            log.warning("second %s, exiting immediately", signal.Signals(signum).name)
            sys.exit(1)
        log.info("received %s, finishing the current job", signal.Signals(signum).name)
        stop.set()

    signal.signal(signal.SIGTERM, handle)
    signal.signal(signal.SIGINT, handle)


def main() -> int:
    """Entry point of pdfsearch-worker.

    Environment variables:
        QUEUE_URL           SQS job queue (required)
        REDIS_URL           Redis to index into (default: see pdfsearch.db)
        AWS_REGION, AWS_PROFILE or AWS access keys: standard AWS SDK settings
        MAX_RECEIVE_COUNT   must match the queue's redrive policy (default 3)
        VISIBILITY_TIMEOUT  seconds a message stays hidden per extension (default 300)
        HEARTBEAT_INTERVAL  seconds between extensions (default 60)
        LOG_LEVEL           default INFO
    """
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

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
    stop = threading.Event()
    install_signal_handlers(stop)
    worker.run(stop)
    return 0


if __name__ == "__main__":
    sys.exit(main())
