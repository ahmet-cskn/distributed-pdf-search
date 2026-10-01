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
