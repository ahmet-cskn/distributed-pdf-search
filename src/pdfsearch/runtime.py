"""Process setup shared by the long-running commands (worker, watcher)."""

import logging
import os
import signal
import sys
import threading
from typing import Any

log = logging.getLogger(__name__)


def configure_logging() -> None:
    """Log to stderr at $LOG_LEVEL (default INFO)."""
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def install_signal_handlers(stop: threading.Event) -> None:
    """SIGTERM (sent by Kubernetes) or Ctrl+C: finish the current work, then exit.

    A second signal exits immediately.
    """

    def handle(signum: int, frame: Any) -> None:
        if stop.is_set():
            log.warning("second %s, exiting immediately", signal.Signals(signum).name)
            sys.exit(1)
        log.info("received %s, finishing the current work", signal.Signals(signum).name)
        stop.set()

    signal.signal(signal.SIGTERM, handle)
    signal.signal(signal.SIGINT, handle)
