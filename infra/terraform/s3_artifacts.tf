resource "aws_s3_bucket" "artifacts" {
  bucket_prefix = "${local.name}-artifacts-"
  force_destroy = false

  tags = merge(local.common_tags, { Name = "${local.name}-artifacts" })
}

# Block all public access.
resource "aws_s3_bucket_public_access_block" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# SSE-S3 (AES256) — no KMS to avoid extra API cost/complexity.
resource "aws_s3_bucket_server_side_encryption_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# Expire run artifacts and config objects after var.artifacts_retention_days days.
# configs/<RUN_ID>.json objects are small but accumulate indefinitely without a
# lifecycle rule — at high run frequency this becomes a cost and hygiene issue.
resource "aws_s3_bucket_lifecycle_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  rule {
    id     = "expire-runs"
    status = "Enabled"

    filter {
      prefix = "runs/"
    }

    expiration {
      days = var.artifacts_retention_days
    }
  }

  rule {
    id     = "expire-configs"
    status = "Enabled"

    filter {
      prefix = "configs/"
    }

    expiration {
      days = var.artifacts_retention_days
    }
  }
}

# TLS-only policy. The policy is non-public (denies anonymous access via
# aws:SecureTransport condition); block_public_policy remains enabled.
resource "aws_s3_bucket_policy" "artifacts_tls_only" {
  bucket = aws_s3_bucket.artifacts.id

  # Must wait for public access block to be set first, otherwise AWS rejects
  # bucket policies that look "public" before the block is in place.
  depends_on = [aws_s3_bucket_public_access_block.artifacts]

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyNonTLS"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource = [
        aws_s3_bucket.artifacts.arn,
        "${aws_s3_bucket.artifacts.arn}/*"
      ]
      Condition = {
        Bool = { "aws:SecureTransport" = "false" }
      }
    }]
  })
}
