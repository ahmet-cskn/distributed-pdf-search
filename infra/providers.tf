# Credentials come from the environment (e.g. AWS_PROFILE), never from this code.
provider "aws" {
  region = var.aws_region

  # Added to every resource this configuration creates, so they are easy to
  # find, filter and attribute in the console and in billing.
  default_tags {
    tags = {
      Project   = var.project_name
      ManagedBy = "terraform"
    }
  }
}
