# IoT Core permissions for the task role.
#
# iot:DescribeEndpoint — resolve the ATS data endpoint at runtime (no resource scoping).
# iot:Publish          — publish messages to IoT topics.
#
# NOTE: verify at smoke-test time that `iot:Publish` is the correct IAM action
# for the boto3 iot-data client publish() call. Update the action name here if
# AccessDenied errors indicate a different action is required.

data "aws_iam_policy_document" "task_iot" {
  statement {
    effect    = "Allow"
    actions   = ["iot:DescribeEndpoint"]
    resources = ["*"] # No resource scope is available for this action.
  }

  statement {
    effect  = "Allow"
    actions = ["iot:Publish"]
    # Scope to all topics in this account/region; narrow to a topic prefix in production.
    resources = ["arn:aws:iot:${var.region}:${data.aws_caller_identity.current.account_id}:topic/*"]
  }
}

resource "aws_iam_policy" "task_iot" {
  name   = "${local.name}-task-iot"
  policy = data.aws_iam_policy_document.task_iot.json
}

resource "aws_iam_role_policy_attachment" "task_role_iot" {
  role       = aws_iam_role.task_role.name
  policy_arn = aws_iam_policy.task_iot.arn
}
