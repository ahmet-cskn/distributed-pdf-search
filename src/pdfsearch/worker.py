"""Worker: turns job messages from SQS into indexed PDFs (docs/DESIGN.md §3.3)."""

import logging
from dataclasses import dataclass
from typing import Any

from redis import Redis

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
