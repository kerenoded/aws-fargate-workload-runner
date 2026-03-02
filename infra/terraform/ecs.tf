resource "aws_ecs_cluster" "this" {
  name = local.name

  # Container Insights: optional, default off.
  # Enable per-environment with var.container_insights = true.
  # Incurs additional CloudWatch metric and log costs.
  setting {
    name  = "containerInsights"
    value = var.container_insights ? "enabled" : "disabled"
  }

  tags = merge(local.common_tags, { Name = local.name })
}

# Security group: no inbound; egress HTTPS + DNS only.
# 443 outbound to 0.0.0.0/0 is required for AWS public API endpoints
# (S3, IoT, SQS, ECR). Restricting to specific CIDRs is impractical
# without VPC endpoints or NAT.
resource "aws_security_group" "task" {
  name        = "${local.name}-task-sg"
  description = "AWFR Fargate task SG (no inbound)"
  vpc_id      = aws_vpc.this.id

  egress {
    description = "HTTPS to AWS public endpoints"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    description = "DNS UDP"
    from_port   = 53
    to_port     = 53
    protocol    = "udp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    description = "DNS TCP (fallback for large responses)"
    from_port   = 53
    to_port     = 53
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(local.common_tags, { Name = "${local.name}-task-sg" })
}

resource "aws_ecs_task_definition" "awfr" {
  family                   = local.name
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "512"
  memory                   = "1024"
  execution_role_arn       = aws_iam_role.task_execution.arn
  task_role_arn            = aws_iam_role.task_role.arn

  runtime_platform {
    cpu_architecture        = "X86_64"
    operating_system_family = "LINUX"
  }

  container_definitions = jsonencode([
    {
      name      = local.container_name
      image     = "${aws_ecr_repository.awfr.repository_url}:${var.image_tag}"
      essential = true

      # ARTIFACTS_BUCKET and SQS_QUEUE_ARNS are static per deployment; never per-run RunTask overrides.
      # Region is not set here — AWS_REGION is injected by Fargate automatically.
      # SQS_QUEUE_ARNS is a comma-separated list of ARNs; empty string when none are configured.
      environment = [
        { name = "ARTIFACTS_BUCKET", value = aws_s3_bucket.artifacts.bucket },
        { name = "SQS_QUEUE_ARNS",   value = join(",", var.sqs_queue_arns) }
      ]

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-region        = var.region
          awslogs-group         = aws_cloudwatch_log_group.awfr.name
          awslogs-stream-prefix = "run"
        }
      }
    }
  ])

  tags = merge(local.common_tags, { Name = local.name })
}
