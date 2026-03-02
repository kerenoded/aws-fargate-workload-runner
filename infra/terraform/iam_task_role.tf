# S3 permissions for the task role.
#
# Reads:  configs/*  (downloads per-run config JSON)
# Writes: runs/*     (uploads metrics.jsonl and summary.json)
#
# ListBucket is scoped to the two prefixes via condition to support
# existence checks without granting broad list access.

data "aws_iam_policy_document" "task_s3" {
  # List bucket (scoped to configs/ and runs/ only)
  statement {
    effect  = "Allow"
    actions = ["s3:ListBucket"]
    resources = [aws_s3_bucket.artifacts.arn]

    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["configs/*", "runs/*"]
    }
  }

  # Read config (configs/* only)
  statement {
    effect    = "Allow"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.artifacts.arn}/configs/*"]
  }

  # Write artifacts (runs/* only); AbortMultipartUpload prevents orphaned parts
  statement {
    effect = "Allow"
    actions = [
      "s3:PutObject",
      "s3:AbortMultipartUpload"
    ]
    resources = ["${aws_s3_bucket.artifacts.arn}/runs/*"]
  }
}

resource "aws_iam_policy" "task_s3" {
  name   = "${local.name}-task-s3"
  policy = data.aws_iam_policy_document.task_s3.json
}

resource "aws_iam_role_policy_attachment" "task_role_s3" {
  role       = aws_iam_role.task_role.name
  policy_arn = aws_iam_policy.task_s3.arn
}
