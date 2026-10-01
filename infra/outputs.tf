output "account_id" {
  description = "AWS account the resources are created in."
  value       = data.aws_caller_identity.current.account_id
}

output "region" {
  description = "AWS Region the resources are created in."
  value       = data.aws_region.current.region
}

output "bucket_name" {
  description = "S3 bucket that PDFs are uploaded to."
  value       = aws_s3_bucket.pdfs.bucket
}

output "queue_url" {
  description = "SQS queue that workers receive jobs from."
  value       = aws_sqs_queue.jobs.url
}

output "dlq_url" {
  description = "Dead-letter queue holding jobs that failed repeatedly."
  value       = aws_sqs_queue.jobs_dlq.url
}

output "iam_users" {
  description = "IAM users per component; create their access keys with the AWS CLI."
  value = {
    watcher = aws_iam_user.watcher.name
    worker  = aws_iam_user.worker.name
    keda    = aws_iam_user.keda.name
  }
}
