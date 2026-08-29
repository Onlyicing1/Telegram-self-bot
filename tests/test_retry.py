from datetime import timedelta

import pytest

from backend.ai.retry import FailureClass, can_retry, classify_failure, retry_delay


def test_retry_classification_and_fail_closed_unknowns():
    assert classify_failure(TimeoutError()).classification == FailureClass.RETRYABLE
    assert classify_failure("rate limit from service").retry is True
    assert classify_failure("invalid payload").classification == FailureClass.UNKNOWN
    assert classify_failure("cancelled by caller").classification == FailureClass.CANCELLED


def test_backoff_is_deterministic_and_attempt_bounded():
    assert retry_delay(1) == timedelta(seconds=30)
    assert retry_delay(2) == timedelta(seconds=60)
    assert can_retry(1) and can_retry(2)
    assert not can_retry(3)
    with pytest.raises(ValueError):
        retry_delay(3)
