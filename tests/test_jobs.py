import json

import pytest

from pdfsearch.jobs import InvalidJobMessage, S3Object, parse_job_message

BUCKET = "pdfsearch-pdfs-123456789012"


def s3_event(key: str, event_name: str = "ObjectCreated:Put") -> dict:
    """One record in the format S3 sends to SQS (fields we don't use trimmed)."""
    return {
        "eventVersion": "2.1",
        "eventSource": "aws:s3",
        "awsRegion": "eu-north-1",
        "eventTime": "2026-10-01T18:58:06.605Z",
        "eventName": event_name,
        "s3": {
            "s3SchemaVersion": "1.0",
            "configurationId": "pdf-uploads-lowercase",
            "bucket": {"name": BUCKET, "arn": f"arn:aws:s3:::{BUCKET}"},
            "object": {"key": key, "size": 837, "eTag": "d41d8cd98f00b204e9800998ecf8427e"},
        },
    }


def body(*records: dict) -> str:
    return json.dumps({"Records": list(records)})


def test_upload_event():
    assert parse_job_message(body(s3_event("e2e-test.pdf"))) == [S3Object(BUCKET, "e2e-test.pdf")]


def test_test_event_carries_no_job():
    # Captured from our bucket when the notification was configured.
    test_event = {
        "Service": "Amazon S3",
        "Event": "s3:TestEvent",
        "Time": "2026-10-01T18:55:45.460Z",
        "Bucket": BUCKET,
        "RequestId": "MXZ7Y95MQQ53AH8T",
        "HostId": "Ux357Pv4mUhF4Su37Q3rYYdQTeXBjQ",
    }
    assert parse_job_message(json.dumps(test_event)) == []


@pytest.mark.parametrize(
    ("encoded", "decoded"),
    [
        ("lecture+1.pdf", "lecture 1.pdf"),  # space
        ("a%2Bb.pdf", "a+b.pdf"),  # literal plus
        ("notes%232.pdf", "notes#2.pdf"),  # hash, allowed in our page ids
        ("%C3%BCbung.PDF", "übung.PDF"),  # non-ASCII, UTF-8 encoded
        ("folder%2Ffile.pdf", "folder/file.pdf"),
    ],
)
def test_keys_are_url_decoded(encoded, decoded):
    assert parse_job_message(body(s3_event(encoded))) == [S3Object(BUCKET, decoded)]


@pytest.mark.parametrize(
    "event_name",
    [
        "ObjectCreated:Put",
        "ObjectCreated:Post",
        "ObjectCreated:Copy",
        "ObjectCreated:CompleteMultipartUpload",
    ],
)
def test_every_kind_of_upload_is_a_job(event_name):
    assert parse_job_message(body(s3_event("a.pdf", event_name))) == [S3Object(BUCKET, "a.pdf")]


def test_non_upload_records_are_skipped():
    assert parse_job_message(body(s3_event("a.pdf", "ObjectRemoved:Delete"))) == []


def test_records_from_other_sources_are_skipped():
    record = s3_event("a.pdf") | {"eventSource": "aws:sqs"}
    assert parse_job_message(body(record)) == []


def test_several_records_in_one_message():
    message = body(s3_event("a.pdf"), s3_event("b.pdf"))
    assert parse_job_message(message) == [S3Object(BUCKET, "a.pdf"), S3Object(BUCKET, "b.pdf")]


@pytest.mark.parametrize(
    "message",
    [
        "not json",
        "[]",
        '"a string"',
        "{}",
        '{"Records": "not a list"}',
        '{"Records": [{"eventSource": "aws:s3"}]}',  # missing fields
        '{"Records": [{"eventSource": "aws:s3", "eventName": "ObjectCreated:Put", "s3": null}]}',
    ],
)
def test_invalid_messages_raise(message):
    with pytest.raises(InvalidJobMessage):
        parse_job_message(message)
