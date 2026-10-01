# Read-only lookups of the account and Region Terraform is operating in.
data "aws_caller_identity" "current" {}

data "aws_region" "current" {}
