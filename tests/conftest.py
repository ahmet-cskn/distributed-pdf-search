import os

import boto3
import pymupdf
import pytest
from moto import mock_aws
from redis.exceptions import ConnectionError

from pdfsearch.db import connect

# Database 15 keeps test data apart from development data in database 0.
TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/15")


@pytest.fixture
def redis_url():
    return TEST_REDIS_URL


@pytest.fixture
def redis(redis_url):
    client = connect(redis_url)
    try:
        client.ping()
    except ConnectionError:
        pytest.fail(
            f"Redis is not reachable at {redis_url}. Start it with: docker compose up -d --wait"
        )
    client.flushdb()
    yield client
    client.flushdb()
    client.close()


def _make_pdf(pages: list[str]) -> bytes:
    """Create a PDF in memory with one page per string ("" = blank page)."""
    with pymupdf.open() as doc:
        for text in pages:
            page = doc.new_page()
            if text:
                page.insert_text((72, 72), text)
        return doc.tobytes()


@pytest.fixture
def make_pdf():
    """Factory fixture: tests call make_pdf(["page 1 text", "page 2 text"])."""
    return _make_pdf


@pytest.fixture
def aws(monkeypatch):
    """Fake AWS (moto) for the duration of a test.

    Fake credentials make sure no test can ever reach a real AWS account,
    even if the shell has AWS_PROFILE set.
    """
    for name in ["AWS_PROFILE", "AWS_DEFAULT_PROFILE"]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-north-1")
    with mock_aws():
        yield


@pytest.fixture
def sqs(aws):
    return boto3.client("sqs")


@pytest.fixture
def queue_url(sqs):
    return sqs.create_queue(QueueName="pdfsearch-jobs")["QueueUrl"]


@pytest.fixture
def s3(aws):
    return boto3.client("s3")


@pytest.fixture
def bucket(s3, sqs, queue_url):
    """A bucket wired to the job queue like infra/events.tf: PDF uploads send jobs."""
    name = "pdfsearch-pdfs-test"
    s3.create_bucket(Bucket=name, CreateBucketConfiguration={"LocationConstraint": "eu-north-1"})
    queue_arn = sqs.get_queue_attributes(QueueUrl=queue_url, AttributeNames=["QueueArn"])
    s3.put_bucket_notification_configuration(
        Bucket=name,
        NotificationConfiguration={
            "QueueConfigurations": [
                {
                    "QueueArn": queue_arn["Attributes"]["QueueArn"],
                    "Events": ["s3:ObjectCreated:*"],
                    "Filter": {"Key": {"FilterRules": [{"Name": "suffix", "Value": suffix}]}},
                }
                for suffix in [".pdf", ".PDF"]
            ]
        },
    )
    # Like real S3, configuring the notification sends a test event. Remove
    # it so tests start with an empty queue.
    for message in sqs.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=10)["Messages"]:
        sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=message["ReceiptHandle"])
    return name
