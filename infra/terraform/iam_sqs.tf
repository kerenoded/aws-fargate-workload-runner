# SQS permissions for the task role.
#
# Attach this via a separate policy so it can be conditionally applied
# (e.g., only when the SQS scenario is in use). One or more queue ARNs
# must be supplied via sqs_queue_arns when enable_sqs_permissions = true.
#
# Usage:
#   terraform apply \
#     -var='enable_sqs_permissions=true' \
#     -var='sqs_queue_arns=["arn:aws:sqs:REGION:ACCOUNT:queue-a","arn:aws:sqs:REGION:ACCOUNT:queue-b"]'

variable "enable_sqs_permissions" {
  description = "Grant the task role SQS send permissions (required for sqs_enqueue scenario)."
  type        = bool
  default     = false
}

variable "sqs_queue_arns" {
  description = "List of SQS queue ARNs the task role may send messages to. At least one ARN is required when enable_sqs_permissions is true."
  type        = list(string)
  default     = []

  validation {
    condition     = !var.enable_sqs_permissions || length(var.sqs_queue_arns) > 0
    error_message = "sqs_queue_arns must contain at least one ARN when enable_sqs_permissions is true."
  }

  validation {
    condition     = alltrue([for arn in var.sqs_queue_arns : can(regex("^arn:aws(-us-gov|-cn)?:sqs:", arn))])
    error_message = "Every entry in sqs_queue_arns must be a valid SQS ARN (arn:aws:sqs:, arn:aws-us-gov:sqs:, or arn:aws-cn:sqs:)."
  }
}

data "aws_iam_policy_document" "task_sqs" {
  count = var.enable_sqs_permissions ? 1 : 0

  statement {
    effect    = "Allow"
    actions   = ["sqs:SendMessage", "sqs:SendMessageBatch", "sqs:GetQueueAttributes"]
    resources = var.sqs_queue_arns
  }

  # cloudwatch:GetMetricData has no resource-level restriction; "*" is required by AWS.
  statement {
    effect    = "Allow"
    actions   = ["cloudwatch:GetMetricData"]
    resources = ["*"]
  }
}

resource "aws_iam_policy" "task_sqs" {
  count  = var.enable_sqs_permissions ? 1 : 0
  name   = "${local.name}-task-sqs"
  policy = data.aws_iam_policy_document.task_sqs[0].json
}

resource "aws_iam_role_policy_attachment" "task_role_sqs" {
  count      = var.enable_sqs_permissions ? 1 : 0
  role       = aws_iam_role.task_role.name
  policy_arn = aws_iam_policy.task_sqs[0].arn
}
