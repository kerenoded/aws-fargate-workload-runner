# Security Policy

## Reporting a Vulnerability

Please **do not** open a public GitHub issue for security vulnerabilities.

Instead, report them privately:

1. Go to the **Security** tab of this repository.
2. Click **Report a vulnerability**.
3. Provide a clear description, steps to reproduce, and potential impact.

We aim to acknowledge reports within 5 business days and issue a fix or mitigation within 30 days.

## Sensitive data

This project runs load tests and may involve:
- Target API endpoints
- SQS queue URLs and ARNs
- IoT topic patterns and device IDs
- AWS credentials / IAM role ARNs

Do **not** paste secrets or real endpoint URLs into issues, PRs, logs, or committed config files.

Use `loadtest/configs/*.local.json` for local-only scenario configs — these files are ignored by git (`*.local.json` in `.gitignore`). The committed files under `loadtest/configs/` are safe placeholder templates only.

## Scope

This policy covers the code in this repository.  
It does not cover third-party dependencies or the AWS services themselves.

## Security Recommendations for Operators

- Store Terraform state in an S3 bucket with versioning and SSE enabled.
- Enable AWS CloudTrail to audit `RunTask` calls.
- Restrict `iam:PassRole` to the specific execution and task role ARNs.
- Rotate IAM credentials regularly or use OIDC / instance profiles.
- Review the `sqs_queue_arn` variable before enabling SQS permissions — scope it to the narrowest possible queue ARN.
