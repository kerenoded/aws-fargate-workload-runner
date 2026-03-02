"""Tests for awfr.config — S3 interactions are mocked."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from awfr.config import ConfigError, load_run_env

_BASE_ENV = {
    "RUN_ID": "run-001",
    "SCENARIO": "sqs_enqueue",
    "CONFIG_S3_URI": "s3://my-bucket/configs/run-001.json",
    "ARTIFACTS_BUCKET": "my-bucket",
}

_VALID_PAYLOAD = json.dumps({"scenario": "sqs_enqueue", "config": {"queue_url": "https://example.com"}})


def _set_env(monkeypatch, overrides=None) -> None:
    for k, v in {**_BASE_ENV, **(overrides or {})}.items():
        monkeypatch.setenv(k, v)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_load_run_env_returns_run_env(monkeypatch):
    _set_env(monkeypatch)
    with patch("awfr.config._download_s3_object", return_value=_VALID_PAYLOAD):
        env = load_run_env()

    assert env.run_id == "run-001"
    assert env.scenario == "sqs_enqueue"
    assert env.artifacts_prefix == "runs/run-001/"


# ---------------------------------------------------------------------------
# Config JSON validation
# ---------------------------------------------------------------------------


def test_load_run_env_invalid_json_raises(monkeypatch):
    _set_env(monkeypatch)
    with patch("awfr.config._download_s3_object", return_value="not-valid-json"):
        with pytest.raises(ConfigError):
            load_run_env()


def test_load_run_env_scenario_mismatch_raises(monkeypatch):
    _set_env(monkeypatch)
    payload = json.dumps({"scenario": "wrong_scenario", "config": {}})
    with patch("awfr.config._download_s3_object", return_value=payload):
        with pytest.raises(ConfigError):
            load_run_env()


def test_load_run_env_non_object_json_raises(monkeypatch):
    _set_env(monkeypatch)
    with patch("awfr.config._download_s3_object", return_value="[1, 2, 3]"):
        with pytest.raises(ConfigError):
            load_run_env()


# ---------------------------------------------------------------------------
# Missing environment variables
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("missing_var", ["RUN_ID", "SCENARIO", "CONFIG_S3_URI", "ARTIFACTS_BUCKET"])
def test_load_run_env_missing_required_env_var_raises(monkeypatch, missing_var):
    _set_env(monkeypatch)
    monkeypatch.delenv(missing_var)
    with pytest.raises(ConfigError):
        load_run_env()


def test_load_run_env_config_uri_bucket_mismatch_raises(monkeypatch):
    # CONFIG_S3_URI bucket must match ARTIFACTS_BUCKET — loader enforces this.
    _set_env(monkeypatch, {"CONFIG_S3_URI": "s3://wrong-bucket/configs/run-001.json"})
    with pytest.raises(ConfigError):
        load_run_env()
