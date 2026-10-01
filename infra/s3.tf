# Bucket for uploaded PDFs. Workers download from it; uploads trigger indexing.

resource "aws_s3_bucket" "pdfs" {
  # Bucket names are global across all AWS accounts; the account ID makes
  # this one unique while keeping it readable.
  bucket = "${var.project_name}-pdfs-${data.aws_caller_identity.current.account_id}"

  # Lets "terraform destroy" delete the bucket even if it still contains PDFs.
  # Acceptable here: every PDF also exists in the local folder it came from.
  force_destroy = true
}

# Never allow public access, whatever a future policy or ACL might say.
resource "aws_s3_bucket_public_access_block" "pdfs" {
  bucket = aws_s3_bucket.pdfs.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Disable ACLs entirely: access is controlled by IAM and bucket policies only.
resource "aws_s3_bucket_ownership_controls" "pdfs" {
  bucket = aws_s3_bucket.pdfs.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

# Encrypt objects at rest with S3-managed keys.
resource "aws_s3_bucket_server_side_encryption_configuration" "pdfs" {
  bucket = aws_s3_bucket.pdfs.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# Interrupted multipart uploads leave invisible parts behind that are still
# billed; clean them up after a day.
resource "aws_s3_bucket_lifecycle_configuration" "pdfs" {
  bucket = aws_s3_bucket.pdfs.id

  rule {
    id     = "abort-incomplete-multipart-uploads"
    status = "Enabled"

    filter {}

    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
}

# Reject any request that does not use HTTPS.
resource "aws_s3_bucket_policy" "pdfs" {
  bucket = aws_s3_bucket.pdfs.id
  policy = data.aws_iam_policy_document.pdfs_bucket.json

  # The public access block must exist first, so this policy is evaluated
  # against it.
  depends_on = [aws_s3_bucket_public_access_block.pdfs]
}

data "aws_iam_policy_document" "pdfs_bucket" {
  statement {
    sid     = "DenyInsecureTransport"
    effect  = "Deny"
    actions = ["s3:*"]
    resources = [
      aws_s3_bucket.pdfs.arn,
      "${aws_s3_bucket.pdfs.arn}/*",
    ]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}
