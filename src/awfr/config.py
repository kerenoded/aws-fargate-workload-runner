"""Environment + S3 config loading and validation."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

import boto3
from botocore.exceptions import ClientError, NoCredentialsError

from awfr import exit_codes


class ConfigError(SystemExit):
    """Raised on configuration / validation errors (exit 2)."""

    def __init__(self, message: str) -> None:
        super().__init__(exit_codes.CONFIG_ERROR)
        self.message = message

    def __str__(self) -> str:
        return self.message


class AuthError(SystemExit):
    """Raised on AWS auth / permission errors (exit 3)."""

    def __init__(self, message: str) -> None:
        super().__init__(exit_codes.AUTH_ERROR)
        self.message = message

    def __str__(self) -> str:
        return self.message


@dataclass
class RunEnv:
    """Resolved environment for a single run."""

    run_id: str
    scenario: str
    config_s3_uri: str
    artifacts_bucket: str
    artifacts_prefix: str  # runs/<RUN_ID>/
    runner_config: dict[str, Any] = field(default_factory=dict)
    scenario_config: dict[str, Any] = field(default_factory=dict)


def _require_env(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        raise ConfigError(f"Required environment variable '{name}' is missing or empty.")
    return val


def load_run_env() -> RunEnv:
    """Read env vars, download S3 config, return a validated RunEnv."""
    # 1. env vars
    run_id = _require_env("RUN_ID")
    scenario = _require_env("SCENARIO")
    config_s3_uri = _require_env("CONFIG_S3_URI")
    artifacts_bucket = _require_env("ARTIFACTS_BUCKET")

    # Defensive region fallback: Fargate injects AWS_REGION automatically.
    # Only propagate it when non-empty — setting AWS_DEFAULT_REGION to an empty
    # string overrides boto3's own resolution chain (profile, ~/.aws/config,
    # instance metadata) and causes confusing NoRegionError in local testing.
    _aws_region = os.environ.get("AWS_REGION", "").strip()
    if _aws_region:
        os.environ.setdefault("AWS_DEFAULT_REGION", _aws_region)

    artifacts_prefix = f"runs/{run_id}/"

    # 2. Validate CONFIG_S3_URI format
    expected_prefix = f"s3://{artifacts_bucket}/configs/"
    if not config_s3_uri.startswith(expected_prefix):
        raise ConfigError(
            f"CONFIG_S3_URI must start with '{expected_prefix}', got: {config_s3_uri}"
        )

    # 3. Download and parse config JSON
    raw = _download_s3_object(config_s3_uri)
    try:
        cfg = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Config JSON at {config_s3_uri} is not valid JSON: {exc}") from exc

    if not isinstance(cfg, dict):
        raise ConfigError("Config JSON must be a JSON object at the top level.")

    # 4. Optional guardrail: scenario field must match if present
    declared_scenario = cfg.get("scenario")
    if declared_scenario is not None and declared_scenario != scenario:
        raise ConfigError(
            f"Config 'scenario' field '{declared_scenario}' does not match "
            f"SCENARIO env var '{scenario}'."
        )

    runner_config: dict[str, Any] = cfg.get("runner") or {}
    scenario_config: dict[str, Any] = cfg.get("config") or {}

    return RunEnv(
        run_id=run_id,
        scenario=scenario,
        config_s3_uri=config_s3_uri,
        artifacts_bucket=artifacts_bucket,
        artifacts_prefix=artifacts_prefix,
        runner_config=runner_config,
        scenario_config=scenario_config,
    )


def _download_s3_object(s3_uri: str) -> str:
    """Download an S3 object and return its content as a string."""
    # Parse s3://bucket/key
    without_scheme = s3_uri[len("s3://"):]
    bucket, _, key = without_scheme.partition("/")
    if not bucket or not key:
        raise ConfigError(f"Cannot parse S3 URI: {s3_uri}")

    s3 = boto3.client("s3")
    try:
        resp = s3.get_object(Bucket=bucket, Key=key)
        return resp["Body"].read().decode("utf-8")
    except NoCredentialsError as exc:
        raise AuthError(f"No AWS credentials available: {exc}") from exc
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("AccessDenied", "403"):
            raise AuthError(
                f"Access denied reading config {s3_uri}: {exc}"
            ) from exc
        if code in ("NoSuchKey", "NoSuchBucket", "404"):
            raise ConfigError(f"Config not found at {s3_uri}: {exc}") from exc
        raise AuthError(f"AWS error reading config {s3_uri}: {exc}") from exc
