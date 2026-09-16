"""Billing exhaustion must not masquerade as an internal error.

Found in the browser, not in a test: the account ran out of credits mid-demo and the chat
showed «Внутрішня помилка. Спробуйте ще раз.» next to a "Повторити спробу" button. Both
halves were wrong. The retry could not possibly succeed, and "internal error" pointed whoever
had to fix it at our code instead of at a billing page.

The shape that caused it is the thing to pin: the SDK raises a BARE `openai.APIError` out of
the streaming decoder with NO status_code and NO error type, so every status-based branch
misses it and it falls through to the catch-all.
"""

from __future__ import annotations

import httpx
import pytest
from openai import APIError, RateLimitError

from app.chat.errors import ERROR_CLASSES, classify_error, is_retryable, user_message

_REQUEST = httpx.Request("POST", "https://api.openai.com/v1/responses")

REAL_MESSAGE = (
    "You have no credits remaining. Add credits to continue using the API at "
    "https://platform.openai.com/settings/organization/billing/."
)


def _api_error(message: str) -> APIError:
    return APIError(message, request=_REQUEST, body=None)


def test_the_exact_error_that_broke_the_demo_is_classified_as_billing() -> None:
    assert classify_error(_api_error(REAL_MESSAGE)) == "upstream_billing"


def test_that_error_carries_no_status_code_which_is_why_it_needed_special_handling() -> None:
    """If this ever starts carrying a status, the message matching can be narrowed."""
    exc = _api_error(REAL_MESSAGE)
    assert getattr(exc, "status_code", None) is None


@pytest.mark.parametrize(
    "message",
    [
        REAL_MESSAGE,
        "Error code: 429 - insufficient_quota: You exceeded your current quota",
        "Your billing account is not active",
    ],
)
def test_the_other_shapes_of_out_of_money_are_caught_too(message: str) -> None:
    assert classify_error(_api_error(message)) == "upstream_billing"


def test_billing_is_never_retryable() -> None:
    """The whole point: a retry button here is a lie."""
    assert is_retryable("upstream_billing") is False


def test_billing_does_not_tell_the_user_it_is_an_internal_error() -> None:
    message = user_message("upstream_billing")
    assert "Внутрішня помилка" not in message
    assert message != user_message("internal")
    # It must say a retry will not help, so nobody sits there pressing the button.
    assert "не допоможе" in message


def test_a_throughput_429_and_a_quota_429_are_different_problems() -> None:
    """Both arrive as RateLimitError with status 429, and they need opposite advice: wait and
    retry vs. stop and go add money. Splitting them is the point of matching on the message."""
    response = httpx.Response(429, request=_REQUEST)

    throttled = RateLimitError(
        "Rate limit reached for gpt-5.6-terra", response=response, body=None
    )
    assert classify_error(throttled) == "rate_limited"
    assert is_retryable("rate_limited") is True

    out_of_money = RateLimitError(
        "insufficient_quota: You exceeded your current quota", response=response, body=None
    )
    assert classify_error(out_of_money) == "upstream_billing"
    assert is_retryable("upstream_billing") is False


def test_an_ordinary_failure_is_still_internal_and_still_retryable() -> None:
    assert classify_error(RuntimeError("something came loose")) == "internal"
    assert is_retryable("internal") is True


def test_every_class_has_a_message_and_every_message_is_ukrainian() -> None:
    for error_class in ERROR_CLASSES:
        message = user_message(error_class)
        assert message.strip(), error_class
        assert any("Ѐ" <= ch <= "ӿ" for ch in message), error_class
