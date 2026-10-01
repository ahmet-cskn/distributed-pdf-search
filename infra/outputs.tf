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
