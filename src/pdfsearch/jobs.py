"""Turning job messages from SQS into the S3 objects to index.

Messages are S3 event notifications (see docs/DESIGN.md §3.2). Their body is
JSON with a "Records" list; each record names the bucket and the object key
of one upload. S3 also sends a one-off "s3:TestEvent" message when the
notification is configured, which carries no job.
"""

import json
from dataclasses import dataclass
from urllib.parse import unquote_plus


class InvalidJobMessage(ValueError):
    """The message body is not an S3 event notification."""


@dataclass(frozen=True)
class S3Object:
    bucket: str
    key: str


def parse_job_message(body: str) -> list[S3Object]:
    """Return the uploaded objects a message refers to.

    Returns an empty list for S3's test event and for records that are not
    uploads. Keys are URL-decoded: S3 encodes them in event notifications,
    e.g. "lecture 1.pdf" arrives as "lecture+1.pdf".

    >>> parse_job_message('{"Service": "Amazon S3", "Event": "s3:TestEvent"}')
    []

    Raises:
        InvalidJobMessage: if the body is not an S3 event notification.
    """
    try:
        event = json.loads(body)
    except json.JSONDecodeError as e:
        raise InvalidJobMessage(f"message body is not JSON: {e}") from e
    if not isinstance(event, dict):
        raise InvalidJobMessage("message body is not a JSON object")

    if event.get("Event") == "s3:TestEvent":
        return []

    records = event.get("Records")
    if not isinstance(records, list):
        raise InvalidJobMessage("message body has no Records list")

    objects = []
    for record in records:
        try:
            if record["eventSource"] != "aws:s3":
                continue
            # ObjectCreated:Put, :Post, :Copy and :CompleteMultipartUpload are
            # all uploads; nothing else is a job.
            if not record["eventName"].startswith("ObjectCreated:"):
                continue
            bucket = record["s3"]["bucket"]["name"]
            key = unquote_plus(record["s3"]["object"]["key"])
        except (KeyError, TypeError) as e:
            raise InvalidJobMessage(f"malformed S3 event record: {e!r}") from e
        objects.append(S3Object(bucket, key))
    return objects
