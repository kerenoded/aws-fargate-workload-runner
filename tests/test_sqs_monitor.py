"""Tests for awfr.sqs_monitor — all AWS calls are mocked."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from awfr.config import ConfigError
from awfr.sqs_monitor import SQSMonitor, _arn_to_queue_url

VALID_ARN = "arn:aws:sqs:eu-west-1:123456789012:my-queue"

# CloudWatch response with no datapoints — keeps tests fast and age output = N/A.
_CW_EMPTY: dict = {"MetricDataResults": []}

# SQS attribute response used across multiple tests.
_SQS_ATTRS = {
    "Attributes": {
        "ApproximateNumberOfMessages": "5",
        "ApproximateNumberOfMessagesNotVisible": "2",
        "ApproximateNumberOfMessagesDelayed": "1",
        # Always include startup attrs so the monitor doesn't hit an unexpected key error.
        "VisibilityTimeout": "30",
        "ReceiveMessageWaitTimeSeconds": "0",
    }
}


# ---------------------------------------------------------------------------
# _arn_to_queue_url
# ---------------------------------------------------------------------------


def test_arn_to_queue_url_valid():
    assert (
        _arn_to_queue_url(VALID_ARN)
        == "https://sqs.eu-west-1.amazonaws.com/123456789012/my-queue"
    )


@pytest.mark.parametrize(
    "bad_arn",
    [
        "not-an-arn",
        "arn:aws:s3:eu-west-1:123456789012:my-queue",   # wrong service
        "arn:aws:sqs:eu-west-1::my-queue",               # empty account
        "arn:aws:sqs:eu-west-1:123456789012:",           # empty queue name
        "arn:aws-fake:sqs:eu-west-1:123456789012:q",    # unknown partition
    ],
)
def test_arn_to_queue_url_invalid_raises(bad_arn):
    with pytest.raises(ConfigError):
        _arn_to_queue_url(bad_arn)


@pytest.mark.parametrize(
    "arn,expected_url",
    [
        # Standard partition
        (
            "arn:aws:sqs:eu-west-1:123456789012:my-queue",
            "https://sqs.eu-west-1.amazonaws.com/123456789012/my-queue",
        ),
        # GovCloud partition — same domain suffix as standard
        (
            "arn:aws-us-gov:sqs:us-gov-east-1:123456789012:my-queue",
            "https://sqs.us-gov-east-1.amazonaws.com/123456789012/my-queue",
        ),
        # China partition — uses amazonaws.com.cn
        (
            "arn:aws-cn:sqs:cn-north-1:123456789012:my-queue",
            "https://sqs.cn-north-1.amazonaws.com.cn/123456789012/my-queue",
        ),
    ],
)
def test_arn_to_queue_url_all_partitions(arn, expected_url):
    assert _arn_to_queue_url(arn) == expected_url


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_client_error(code: str) -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": "mocked"}}, "GetQueueAttributes"
    )


def _make_monitor(arns=None, interval=5) -> tuple[SQSMonitor, MagicMock, MagicMock]:
    """Return a SQSMonitor with both boto3 clients (SQS and CloudWatch) mocked."""
    arns = arns or [VALID_ARN]
    mock_sqs = MagicMock()
    mock_cw = MagicMock()

    def _fake_client(svc, **_):
        if svc == "sqs":
            return mock_sqs
        if svc == "cloudwatch":
            return mock_cw
        raise AssertionError(f"unexpected boto3 client: {svc}")

    with patch("awfr.sqs_monitor.boto3.client", side_effect=_fake_client):
        monitor = SQSMonitor(arns=arns, interval_seconds=interval, run_id="test-run")

    return monitor, mock_sqs, mock_cw


# ---------------------------------------------------------------------------
# SQSMonitor._poll_all — happy path
# ---------------------------------------------------------------------------


def test_poll_all_prints_queue_stats(capsys):
    monitor, mock_sqs, mock_cw = _make_monitor()
    mock_cw.get_metric_data.return_value = _CW_EMPTY
    mock_sqs.get_queue_attributes.return_value = _SQS_ATTRS

    monitor._poll_all()

    out = capsys.readouterr().out
    assert "visible=5" in out
    assert "in_flight=2" in out
    assert "delayed=1" in out
    assert "my-queue" in out
    assert "run_id=test-run" in out


# ---------------------------------------------------------------------------
# SQSMonitor._poll_all — startup config line
# ---------------------------------------------------------------------------


def test_poll_all_prints_startup_config_line_on_first_poll(capsys):
    monitor, mock_sqs, mock_cw = _make_monitor()
    mock_cw.get_metric_data.return_value = _CW_EMPTY
    mock_sqs.get_queue_attributes.return_value = _SQS_ATTRS

    monitor._poll_all()

    out = capsys.readouterr().out
    assert "[SQS][config]" in out
    assert "visibility_timeout_sec=30" in out
    assert "receive_wait_sec=0" in out


def test_poll_all_startup_config_line_not_repeated_on_second_poll(capsys):
    monitor, mock_sqs, mock_cw = _make_monitor()
    mock_cw.get_metric_data.return_value = _CW_EMPTY
    mock_sqs.get_queue_attributes.return_value = _SQS_ATTRS

    monitor._poll_all()  # first poll — config line printed
    capsys.readouterr()  # discard

    monitor._poll_all()  # second poll — config line must NOT appear again
    out2 = capsys.readouterr().out
    assert "[SQS][config]" not in out2
    # Regular poll line should still appear
    assert "visible=" in out2


# ---------------------------------------------------------------------------
# SQSMonitor._poll_all — AccessDenied
# ---------------------------------------------------------------------------


def test_poll_all_access_denied_prints_warning(capsys):
    monitor, mock_sqs, mock_cw = _make_monitor()
    mock_cw.get_metric_data.return_value = _CW_EMPTY
    mock_sqs.get_queue_attributes.side_effect = _make_client_error("AccessDenied")

    monitor._poll_all()

    out = capsys.readouterr().out
    assert "AccessDenied" in out
    assert "my-queue" in out


def test_poll_all_access_denied_does_not_raise():
    monitor, mock_sqs, mock_cw = _make_monitor()
    mock_cw.get_metric_data.return_value = _CW_EMPTY
    mock_sqs.get_queue_attributes.side_effect = _make_client_error("AccessDenied")

    monitor._poll_all()  # must not raise


def test_poll_all_access_denied_skips_queue_on_subsequent_polls():
    monitor, mock_sqs, mock_cw = _make_monitor()
    mock_cw.get_metric_data.return_value = _CW_EMPTY
    mock_sqs.get_queue_attributes.side_effect = _make_client_error("AccessDenied")

    monitor._poll_all()  # queue added to _access_denied

    mock_sqs.get_queue_attributes.reset_mock()
    mock_cw.get_metric_data.reset_mock()

    monitor._poll_all()  # active list is now empty → no SQS or CW calls
    mock_sqs.get_queue_attributes.assert_not_called()
    mock_cw.get_metric_data.assert_not_called()


# ---------------------------------------------------------------------------
# SQSMonitor._poll_all — throttling
# ---------------------------------------------------------------------------


def test_poll_all_throttle_does_not_print_warning_and_does_not_raise(capsys):
    monitor, mock_sqs, mock_cw = _make_monitor()
    mock_cw.get_metric_data.return_value = _CW_EMPTY
    mock_sqs.get_queue_attributes.side_effect = _make_client_error("ThrottlingException")

    monitor._poll_all()  # must not raise

    out = capsys.readouterr().out
    assert "WARNING" not in out

