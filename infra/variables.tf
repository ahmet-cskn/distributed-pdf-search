variable "aws_region" {
  description = "AWS Region for all resources. Accounts on AWS's new project-based experience are pinned to one Region (eu-north-1 for European contact addresses)."
  type        = string
  default     = "eu-north-1"
}

variable "project_name" {
  description = "Prefix for resource names and value of the Project tag."
  type        = string
  default     = "pdfsearch"
}

variable "budget_alert_email" {
  description = "Email address that receives budget alerts. Set it in terraform.tfvars (git-ignored)."
  type        = string

  validation {
    condition     = can(regex("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$", var.budget_alert_email))
    error_message = "budget_alert_email must be an email address."
  }
}

variable "monthly_budget_usd" {
  description = "Monthly usage (in USD) that triggers budget alerts."
  type        = string
  default     = "5"
}
