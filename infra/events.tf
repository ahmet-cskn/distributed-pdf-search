# Wiring S3 to SQS: every uploaded PDF produces one job message.

# Queue policy: who may do what with the job queue.
resource "aws_sqs_queue_policy" "jobs" {
  queue_url = aws_sqs_queue.jobs.id
  policy    = data.aws_iam_policy_document.jobs_queue.json
}

data "aws_iam_policy_document" "jobs_queue" {
  # S3 may send messages, but only on behalf of our bucket in our account.
  # Without these conditions any S3 bucket, including someone else's,
  # could be configured to send messages to this queue.
  statement {
    sid       = "AllowS3EventsFromPdfBucket"
    effect    = "Allow"
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.jobs.arn]

    principals {
      type        = "Service"
      identifiers = ["s3.amazonaws.com"]
    }

    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = [aws_s3_bucket.pdfs.arn]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }

  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["sqs:*"]
    resources = [aws_sqs_queue.jobs.arn]

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

resource "aws_s3_bucket_notification" "pdf_uploads" {
  bucket = aws_s3_bucket.pdfs.id

  # Suffix filters are case-sensitive, so both common spellings are listed.
  # Mixed case such as ".Pdf" is not matched.
  queue {
    id            = "pdf-uploads-lowercase"
    queue_arn     = aws_sqs_queue.jobs.arn
    events        = ["s3:ObjectCreated:*"]
    filter_suffix = ".pdf"
  }

  queue {
    id            = "pdf-uploads-uppercase"
    queue_arn     = aws_sqs_queue.jobs.arn
    events        = ["s3:ObjectCreated:*"]
    filter_suffix = ".PDF"
  }

  # S3 checks that it may write to the queue when the notification is
  # created, so the queue policy must exist first.
  depends_on = [aws_sqs_queue_policy.jobs]
}
