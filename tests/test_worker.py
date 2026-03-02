"""Tests for awfr.worker — all AWS and filesystem interactions are mocked."""

from __future__ import annotations

from contextlib import ExitStack
from unittest.mock import MagicMock, patch

from awfr import exit_codes
from awfr.config import AuthError, ConfigError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_run_env(scenario: str = "sqs_enqueue", runner_config: dict | None = None) -> MagicMock:
    env = MagicMock()
    env.run_id = "test-run-001"
    env.scenario = scenario
    env.config_s3_uri = "s3://bucket/configs/test-run-001.json"
    env.artifacts_bucket = "bucket"
    env.artifacts_prefix = "runs/test-run-001/"
    env.runner_config = runner_config or {}
    env.scenario_config = {}
    return env


def _run_worker(scenario_fn: object = None, run_env: object = None) -> int:
    """Run the worker with all external dependencies mocked. Returns exit code."""
    from awfr.worker import run

    mock_run_env = run_env or _make_run_env()
    mock_scenario = scenario_fn or MagicMock(return_value={"sends_succeeded": 10})

    with ExitStack() as stack:
        stack.enter_context(patch("awfr.worker.load_run_env", return_value=mock_run_env))
        stack.enter_context(patch("awfr.worker.SCENARIOS", {mock_run_env.scenario: mock_scenario}))
        stack.enter_context(patch("awfr.worker.S3Uploader"))
        stack.enter_context(patch("awfr.worker.MetricsWriter"))
        return run()


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_run_success_returns_zero():
    assert _run_worker() == exit_codes.SUCCESS


# ---------------------------------------------------------------------------
# load_run_env failures
# ---------------------------------------------------------------------------


def test_run_load_env_config_error_returns_2():
    from awfr.worker import run

    with patch("awfr.worker.load_run_env", side_effect=ConfigError("bad config")):
        assert run() == exit_codes.CONFIG_ERROR


def test_run_load_env_auth_error_returns_3():
    from awfr.worker import run

    with patch("awfr.worker.load_run_env", side_effect=AuthError("no creds")):
        assert run() == exit_codes.AUTH_ERROR


# ---------------------------------------------------------------------------
# Unknown scenario
# ---------------------------------------------------------------------------


def test_run_unknown_scenario_returns_2():
    from awfr.worker import run

    mock_run_env = _make_run_env(scenario="nonexistent_scenario")
    with ExitStack() as stack:
        stack.enter_context(patch("awfr.worker.load_run_env", return_value=mock_run_env))
        stack.enter_context(patch("awfr.worker.SCENARIOS", {}))
        stack.enter_context(patch("awfr.worker.S3Uploader"))
        stack.enter_context(patch("awfr.worker.MetricsWriter"))
        assert run() == exit_codes.CONFIG_ERROR


# ---------------------------------------------------------------------------
# Scenario-level errors
# ---------------------------------------------------------------------------


def test_run_scenario_config_error_returns_2():
    def _bad(**_):
        raise ConfigError("bad scenario config")

    assert _run_worker(scenario_fn=_bad) == exit_codes.CONFIG_ERROR


def test_run_scenario_auth_error_returns_3():
    def _bad(**_):
        raise AuthError("access denied")

    assert _run_worker(scenario_fn=_bad) == exit_codes.AUTH_ERROR


def test_run_scenario_runtime_error_returns_4():
    def _bad(**_):
        raise RuntimeError("something exploded")

    assert _run_worker(scenario_fn=_bad) == exit_codes.RUNTIME_ERROR


def test_run_scenario_keyboard_interrupt_returns_4():
    """KeyboardInterrupt (Ctrl-C / SIGTERM) must upload artifacts and return RUNTIME_ERROR."""

    def _bad(**_):
        raise KeyboardInterrupt

    assert _run_worker(scenario_fn=_bad) == exit_codes.RUNTIME_ERROR


# ---------------------------------------------------------------------------
# Artifact upload is always attempted (best-effort)
# ---------------------------------------------------------------------------


def test_run_success_calls_upload_summary():
    """S3Uploader.upload_summary must be called even on a successful run."""
    from awfr.worker import run

    mock_run_env = _make_run_env()
    mock_uploader_instance = MagicMock()
    mock_uploader_cls = MagicMock(return_value=mock_uploader_instance)

    with ExitStack() as stack:
        stack.enter_context(patch("awfr.worker.load_run_env", return_value=mock_run_env))
        stack.enter_context(
            patch("awfr.worker.SCENARIOS", {mock_run_env.scenario: MagicMock(return_value={})})
        )
        stack.enter_context(patch("awfr.worker.S3Uploader", mock_uploader_cls))
        stack.enter_context(patch("awfr.worker.MetricsWriter"))
        run()

    mock_uploader_instance.upload_summary.assert_called_once()


def test_run_scenario_error_still_calls_upload_summary():
    """upload_summary must be called even when the scenario raises."""
    from awfr.worker import run

    mock_run_env = _make_run_env()
    mock_uploader_instance = MagicMock()
    mock_uploader_cls = MagicMock(return_value=mock_uploader_instance)

    def _bad(**_):
        raise RuntimeError("boom")

    with ExitStack() as stack:
        stack.enter_context(patch("awfr.worker.load_run_env", return_value=mock_run_env))
        stack.enter_context(patch("awfr.worker.SCENARIOS", {mock_run_env.scenario: _bad}))
        stack.enter_context(patch("awfr.worker.S3Uploader", mock_uploader_cls))
        stack.enter_context(patch("awfr.worker.MetricsWriter"))
        run()

    mock_uploader_instance.upload_summary.assert_called_once()
