"""One function, one closed vocabulary.

`error_class` is a CLOSED set so that `GROUP BY error_class` means something. v1 had no error
taxonomy and no logs at all: seven requests ate the SDK's 600s read timeout twice, silently,
with nothing at ERROR level; 17 opaque HTTP 500s could not be correlated with anything.

Every value below is derived from v1's measured failure set, so the table is a list of things
that actually happened, not a list of things that could.
"""

from __future__ import annotations

from typing import Final, Literal, get_args

from agents.exceptions import (
    AgentsException,
    InputGuardrailTripwireTriggered,
    MaxTurnsExceeded,
    ModelBehaviorError,
    OutputGuardrailTripwireTriggered,
)
from chatkit.store import NotFoundError

__all__ = ["ERROR_CLASSES", "ErrorClass", "classify_error", "is_retryable"]

ErrorClass = Literal[
    "provenance_violation",  # E4 rejected an emitted code — v1's measured 36%, never detected
    "code_not_in_dataset",  # E2 — ranked finding #1 in the v1 research, never checked
    "code_not_leaf",  # E3 — `is_final` was a prompt promise
    "schema_violation",  # structured output failed validation — 0 in 4,343, record anyway
    "max_turns_exceeded",  # MaxTurnsExceeded
    "upstream_5xx",  # OpenAI 5xx after the SDK's own retries — 17x500 + 4x502 + 1x520
    "upstream_4xx",  # incl. the stale-item 404 "Item with id 'rs_…' not found"
    "rate_limited",  # 429
    "timeout",  # our own wall-clock budget; v1 had none and its p99 was 141s
    "guardrail_tripwire",
    "cancelled",  # client disconnected mid-stream
    "not_found",  # thread/item is gone, or was never the caller's
    "internal",  # everything else. v1's catch-all, 5 user-visible
]

ERROR_CLASSES: Final[tuple[str, ...]] = get_args(ErrorClass)

# Retry advice for the user-facing error event. "Not retryable" means a retry would fail the
# same way; getting this wrong costs ~5 silent client re-runs (§10.5).
_NON_RETRYABLE: Final[frozenset[str]] = frozenset(
    {
        "provenance_violation",
        "code_not_in_dataset",
        "code_not_leaf",
        "schema_violation",
        "guardrail_tripwire",
        "cancelled",
        "not_found",
        "upstream_4xx",
    }
)


def _status_of(exc: BaseException) -> int | None:
    """HTTP status from an openai/httpx error without importing either package.

    Deliberately duck-typed: `openai.APIStatusError` carries `.status_code`, httpx carries
    `.response.status_code`, and importing the exception hierarchy here would tie this module
    to a client library it otherwise never touches.
    """
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    return status if isinstance(status, int) else None


def classify_error(exc: BaseException) -> ErrorClass:
    """Map any exception raised during a turn onto the closed vocabulary."""
    if isinstance(exc, NotFoundError):
        return "not_found"
    if isinstance(exc, InputGuardrailTripwireTriggered | OutputGuardrailTripwireTriggered):
        return "guardrail_tripwire"
    if isinstance(exc, MaxTurnsExceeded):
        return "max_turns_exceeded"
    if isinstance(exc, TimeoutError):
        return "timeout"
    # asyncio.CancelledError inherits BaseException, not Exception, so it reaches here by
    # name rather than by isinstance against the Exception hierarchy.
    if type(exc).__name__ in {"CancelledError", "ClientDisconnect"}:
        return "cancelled"
    if type(exc).__name__ in {"APITimeoutError", "ReadTimeout", "ConnectTimeout"}:
        return "timeout"
    if type(exc).__name__ in {"ValidationError", "ModelBehaviorError"} or isinstance(
        exc, ModelBehaviorError
    ):
        return "schema_violation"

    status = _status_of(exc)
    if status == 429:
        return "rate_limited"
    if status is not None and 500 <= status < 600:
        return "upstream_5xx"
    if status is not None and 400 <= status < 500:
        return "upstream_4xx"

    if type(exc).__name__ in {"APIConnectionError", "ConnectError"}:
        return "upstream_5xx"
    if isinstance(exc, AgentsException):
        return "internal"
    return "internal"


def is_retryable(error_class: ErrorClass) -> bool:
    """Whether the user-facing error event should offer a retry."""
    return error_class not in _NON_RETRYABLE
