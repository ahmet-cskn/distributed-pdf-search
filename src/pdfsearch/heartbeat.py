"""Keeping a received SQS message hidden while it is being processed."""

import logging
import threading
from types import TracebackType
from typing import Any, Self

log = logging.getLogger(__name__)


class VisibilityHeartbeat:
    """Periodically extends a message's visibility timeout in the background.

    A received message is hidden from other workers only for the queue's
    visibility timeout. Processing a large PDF can take longer; without a
    heartbeat the message would reappear and a second worker would start on
    the same file. Use as a context manager around the processing:

        with VisibilityHeartbeat(sqs, queue_url, receipt_handle):
            process(message)
    """

    def __init__(
        self,
        sqs: Any,
        queue_url: str,
        receipt_handle: str,
        *,
        interval: float = 60,
        extend_to: int = 300,
    ) -> None:
        """Every `interval` seconds, hide the message for `extend_to` more seconds.

        `interval` must be well below `extend_to`, so a single failed or late
        extension does not let the message become visible.
        """
        self._sqs = sqs
        self._queue_url = queue_url
        self._receipt_handle = receipt_handle
        self._interval = interval
        self._extend_to = extend_to
        self._stop = threading.Event()
        # Daemon thread: never keeps the process alive on its own.
        self._thread = threading.Thread(target=self._run, name="visibility-heartbeat", daemon=True)

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._stop.set()
        self._thread.join()

    def _run(self) -> None:
        # wait() returns True as soon as stop is set, so exiting the context
        # never waits for a full interval.
        while not self._stop.wait(self._interval):
            try:
                self._sqs.change_message_visibility(
                    QueueUrl=self._queue_url,
                    ReceiptHandle=self._receipt_handle,
                    VisibilityTimeout=self._extend_to,
                )
            except Exception:
                # Keep trying: the next extension may succeed in time. If the
                # message does reappear, a second worker processes it again,
                # which indexing tolerates (it is idempotent).
                log.warning("could not extend message visibility", exc_info=True)
