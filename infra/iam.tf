# One IAM user per component, each allowed only what that component does.
# Access keys are created separately with the AWS CLI when a component is
# deployed, so secrets never end up in Terraform state.

locals {
  pdf_objects = "${aws_s3_bucket.pdfs.arn}/*"
}

# Watcher: uploads PDFs, and lists the bucket to find files it still has to
# upload after a restart.
resource "aws_iam_user" "watcher" {
  name = "${var.project_name}-watcher"
  path = "/${var.project_name}/"
}

resource "aws_iam_user_policy" "watcher" {
  name   = "upload-pdfs"
  user   = aws_iam_user.watcher.name
  policy = data.aws_iam_policy_document.watcher.json
}

data "aws_iam_policy_document" "watcher" {
  statement {
    sid       = "ListBucket"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.pdfs.arn]
  }

  statement {
    sid = "UploadObjects"
    # PutObject also covers the steps of a multipart upload; aborting one
    # that failed needs its own permission.
    actions   = ["s3:PutObject", "s3:AbortMultipartUpload"]
    resources = [local.pdf_objects]
  }
}

# Worker: receives jobs, downloads the PDF, and deletes the job when done.
resource "aws_iam_user" "worker" {
  name = "${var.project_name}-worker"
  path = "/${var.project_name}/"
}

resource "aws_iam_user_policy" "worker" {
  name   = "process-jobs"
  user   = aws_iam_user.worker.name
  policy = data.aws_iam_policy_document.worker.json
}

data "aws_iam_policy_document" "worker" {
  statement {
    sid       = "DownloadObjects"
    actions   = ["s3:GetObject"]
    resources = [local.pdf_objects]
  }

  statement {
    sid = "ConsumeJobs"
    actions = [
      "sqs:ReceiveMessage",
      "sqs:DeleteMessage",
      "sqs:ChangeMessageVisibility",
    ]
    resources = [aws_sqs_queue.jobs.arn]
  }
}

# KEDA: reads the queue length to decide how many workers to run.
resource "aws_iam_user" "keda" {
  name = "${var.project_name}-keda"
  path = "/${var.project_name}/"
}

resource "aws_iam_user_policy" "keda" {
  name   = "read-queue-length"
  user   = aws_iam_user.keda.name
  policy = data.aws_iam_policy_document.keda.json
}

data "aws_iam_policy_document" "keda" {
  statement {
    sid       = "ReadQueueAttributes"
    actions   = ["sqs:GetQueueAttributes"]
    resources = [aws_sqs_queue.jobs.arn]
  }
}
