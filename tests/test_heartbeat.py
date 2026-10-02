import threading
import time

from pdfsearch.heartbeat import VisibilityHeartbeat


def receive(sqs, queue_url, visibility_timeout=None):
    kwargs = {"QueueUrl": queue_url, "MaxNumberOfMessages": 1}
    if visibility_timeout is not None:
        kwargs["VisibilityTimeout"] = visibility_timeout
    return sqs.receive_message(**kwargs).get("Messages", [])


def test_keeps_message_hidden_past_its_timeout(sqs, queue_url):
    sqs.send_message(QueueUrl=queue_url, MessageBody="job")
    [message] = receive(sqs, queue_url, visibility_timeout=1)

    with VisibilityHeartbeat(sqs, queue_url, message["ReceiptHandle"], interval=0.3, extend_to=1):
        time.sleep(2)  # twice the original timeout
        # Another worker must not get the message while it is being processed.
        assert receive(sqs, queue_url) == []

    # Once the heartbeat stops, the message reappears after the last extension.
    time.sleep(1.5)
    [again] = receive(sqs, queue_url)
    assert again["MessageId"] == message["MessageId"]


def test_without_heartbeat_message_reappears(sqs, queue_url):
    # Control experiment for the test above.
    sqs.send_message(QueueUrl=queue_url, MessageBody="job")
    receive(sqs, queue_url, visibility_timeout=1)
    time.sleep(1.5)
    assert len(receive(sqs, queue_url)) == 1


def test_exit_does_not_wait_for_the_interval(sqs, queue_url):
    start = time.monotonic()
    with VisibilityHeartbeat(sqs, queue_url, "unused", interval=60):
        pass
    assert time.monotonic() - start < 1


class FlakySqs:
    """Fails the first extension, then succeeds."""

    def __init__(self):
        self.calls = 0
        self.succeeded = threading.Event()

    def change_message_visibility(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            raise ConnectionError("network hiccup")
        self.succeeded.set()


def test_keeps_going_after_a_failed_extension():
    sqs = FlakySqs()
    with VisibilityHeartbeat(sqs, "queue", "receipt", interval=0.05):
        assert sqs.succeeded.wait(timeout=2)
    assert sqs.calls >= 2
