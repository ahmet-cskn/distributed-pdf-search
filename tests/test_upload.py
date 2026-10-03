import os

from pdfsearch.jobs import parse_job_message
from pdfsearch.upload import list_bucket_keys, upload_files


def received_jobs(sqs, queue_url):
    """Drain the job queue and return the uploaded keys it announced."""
    keys = []
    while True:
        messages = sqs.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=10).get(
            "Messages", []
        )
        if not messages:
            return sorted(keys)
        for message in messages:
            keys.extend(obj.key for obj in parse_job_message(message["Body"]))
            sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=message["ReceiptHandle"])


def test_list_empty_bucket(s3, bucket):
    assert list_bucket_keys(s3, bucket) == set()


def test_list_reads_every_page(s3, bucket):
    # More than the 1000 keys a single ListObjectsV2 call returns.
    for i in range(1005):
        s3.put_object(Bucket=bucket, Key=f"doc{i:04}.txt", Body=b"")
    keys = list_bucket_keys(s3, bucket)
    assert len(keys) == 1005
    assert "doc1004.txt" in keys


def test_uploads_under_filename_and_creates_jobs(s3, sqs, queue_url, bucket, tmp_path):
    paths = []
    for name in ["a.pdf", "B.PDF", "lecture 3 übung.pdf"]:
        path = tmp_path / name
        path.write_bytes(f"content of {name}".encode())
        paths.append(path)

    result = upload_files(s3, bucket, paths)

    assert sorted(result.uploaded) == sorted(p.name for p in paths)
    assert result.failed == {}
    for path in paths:
        obj = s3.get_object(Bucket=bucket, Key=path.name)
        assert obj["Body"].read() == path.read_bytes()
        assert obj["ContentType"] == "application/pdf"
    # Each upload announced itself to the workers, decoded back to its name.
    assert received_jobs(sqs, queue_url) == sorted(p.name for p in paths)


def test_large_file_uses_multipart_upload(s3, sqs, queue_url, bucket, tmp_path):
    # Above upload_file's default multipart threshold of 8 MB.
    path = tmp_path / "big.pdf"
    path.write_bytes(os.urandom(9 * 1024 * 1024))

    result = upload_files(s3, bucket, [path])

    assert result.uploaded == ["big.pdf"]
    obj = s3.head_object(Bucket=bucket, Key="big.pdf")
    assert obj["ContentLength"] == 9 * 1024 * 1024
    assert "-" in obj["ETag"]  # multipart uploads have ETags like "abc...-2"
    # moto announces a multipart upload twice (ObjectCreated:Put and
    # :CompleteMultipartUpload); real S3 sends only the latter. Duplicates are
    # harmless either way, since processing a file twice is idempotent.
    assert set(received_jobs(sqs, queue_url)) == {"big.pdf"}


def test_failed_upload_does_not_stop_others(s3, bucket, tmp_path):
    good = tmp_path / "good.pdf"
    good.write_bytes(b"content")
    missing = tmp_path / "missing.pdf"  # deleted before it could be uploaded

    result = upload_files(s3, bucket, [good, missing])

    assert result.uploaded == ["good.pdf"]
    assert list(result.failed) == ["missing.pdf"]
    assert list_bucket_keys(s3, bucket) == {"good.pdf"}


def test_missing_bucket_fails_every_upload(s3, aws, tmp_path):
    path = tmp_path / "a.pdf"
    path.write_bytes(b"content")

    result = upload_files(s3, "no-such-bucket-pdfsearch", [path])

    assert result.uploaded == []
    assert list(result.failed) == ["a.pdf"]


def test_nothing_to_upload(s3, bucket):
    result = upload_files(s3, bucket, [])
    assert result.uploaded == []
    assert result.failed == {}
