resource "aws_cloudwatch_log_group" "awfr" {
  name              = "/ecs/${local.name}"
  retention_in_days = 7

  tags = merge(local.common_tags, { Name = "/ecs/${local.name}" })
}
