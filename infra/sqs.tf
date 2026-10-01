# Job queue: one message per uploaded PDF, consumed by the workers.

resource "aws_sqs_queue" "jobs" {
  name = "${var.project_name}-jobs"

  # How long a received message stays hidden from other workers. Workers
  # extend it with a heartbeat while processing large files.
  visibility_timeout_seconds = 300

  # Long polling: a receive call waits up to 20 s for a message instead of
  # returning empty immediately, which saves requests while the queue is idle.
  receive_wait_time_seconds = 20

  # Default retention (4 days); kept shorter than the DLQ's on purpose, see below.
  message_retention_seconds = 345600

  sqs_managed_sse_enabled = true

  # After 3 receives without being deleted, a message moves to the DLQ.
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.jobs_dlq.arn
    maxReceiveCount     = 3
  })
}

# Dead-letter queue: messages for PDFs that failed 3 times, kept for inspection.
resource "aws_sqs_queue" "jobs_dlq" {
  name = "${var.project_name}-jobs-dlq"

  # 14 days, the maximum. A message keeps its original enqueue time when it
  # moves to the DLQ, so the DLQ's retention must be longer than the main
  # queue's, or failed messages could expire right after arriving.
  message_retention_seconds = 1209600

  sqs_managed_sse_enabled = true
}

# Only the job queue may use this DLQ.
resource "aws_sqs_queue_redrive_allow_policy" "jobs_dlq" {
  queue_url = aws_sqs_queue.jobs_dlq.id

  redrive_allow_policy = jsonencode({
    redrivePermission = "byQueue"
    sourceQueueArns   = [aws_sqs_queue.jobs.arn]
  })
}
