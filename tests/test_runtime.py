import os
import signal
import threading

import pytest

from pdfsearch.runtime import install_signal_handlers


def test_signal_requests_a_graceful_stop(preserve_signal_handlers):
    stop = threading.Event()
    install_signal_handlers(stop)

    os.kill(os.getpid(), signal.SIGTERM)
    assert stop.is_set()

    # A second signal exits right away.
    with pytest.raises(SystemExit):
        os.kill(os.getpid(), signal.SIGINT)
