output "ecr_repo_url" {
  description = "ECR repository URL for the runner image."
  value       = aws_ecr_repository.awfr.repository_url
}

output "ecs_cluster_arn" {
  description = "ECS cluster ARN."
  value       = aws_ecs_cluster.this.arn
}

output "task_definition_arn" {
  description = "ECS task definition ARN (base revision; may be overridden per-run with --image-tag)."
  value       = aws_ecs_task_definition.awfr.arn
}

output "container_name" {
  description = "Container name inside the task definition."
  value       = local.container_name
}

output "public_subnet_ids" {
  description = "Public subnet IDs for Fargate task networking."
  value       = [for s in aws_subnet.public : s.id]
}

output "task_security_group_id" {
  description = "Security group ID attached to the Fargate task ENI."
  value       = aws_security_group.task.id
}

output "log_group_name" {
  description = "CloudWatch Logs group for task stdout."
  value       = aws_cloudwatch_log_group.awfr.name
}

output "artifacts_bucket_name" {
  description = "S3 bucket where configs and run artifacts are stored."
  value       = aws_s3_bucket.artifacts.bucket
}

output "sqs_queue_arns" {
  description = "SQS queue ARNs the task role is permitted to send to (empty when enable_sqs_permissions=false)."
  value       = var.sqs_queue_arns
}
