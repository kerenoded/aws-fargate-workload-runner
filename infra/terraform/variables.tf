variable "project" {
  description = "Project name; used as a prefix for all resources."
  type        = string
  default     = "awfr"
}

variable "region" {
  description = "AWS region for all resources."
  type        = string
  default     = "eu-west-1"
}

variable "image_tag" {
  description = "ECR image tag to use in the base task definition."
  type        = string
  default     = "latest"
}

variable "artifacts_retention_days" {
  description = "Number of days before run artifacts under runs/* expire."
  type        = number
  default     = 30
}

variable "container_insights" {
  description = "Enable ECS Container Insights (incurs CloudWatch cost)."
  type        = bool
  default     = false
}
