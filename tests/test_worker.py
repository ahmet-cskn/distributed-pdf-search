import json

import pytest

from pdfsearch.search import PageMatch, search
from pdfsearch.status import FileInfo, FileStatus, get_status
from pdfsearch.worker import Worker


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
