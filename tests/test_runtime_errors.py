import asyncio

from manyselves.runtime.runtime_errors import classify_runtime_error


class HttpFailureError(RuntimeError):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


def test_rate_limit_is_retryable_but_authentication_is_not():
    rate_limit = classify_runtime_error(HttpFailureError(429, "request limit"), retry_number=1)
    auth = classify_runtime_error(HttpFailureError(401, "bad key"), retry_number=1)

    assert rate_limit.category == "rate_limit"
    assert rate_limit.retryable is True
    assert rate_limit.delay_seconds > 0
    assert auth.category == "authentication"
    assert auth.retryable is False


def test_timeout_connection_server_and_validation_are_classified_conservatively():
    assert classify_runtime_error(asyncio.TimeoutError()).category == "timeout"
    assert classify_runtime_error(ConnectionError("connection reset")).category == "connection"
    assert classify_runtime_error(HttpFailureError(503, "down")).category == "server"

    validation = classify_runtime_error(ValueError("bad evidence"))
    assert validation.category == "validation"
    assert validation.retryable is False


def test_unknown_error_is_visible_but_not_retried():
    policy = classify_runtime_error(RuntimeError("unexpected internal state"))

    assert policy.category == "unknown"
    assert policy.retryable is False


def test_conflict_requires_reconciliation_instead_of_automatic_retry():
    policy = classify_runtime_error(
        HttpFailureError(409, "request id already exists")
    )

    assert policy.category == "conflict"
    assert policy.retryable is False
    assert policy.delay_seconds == 0
