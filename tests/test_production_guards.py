"""The boot guards for settings that fail silently in production.

An earlier revision of the M3 guard sat inside the empty-SESSION_SECRET fallback, after a
`raise`, and could never run; a smoke test with the placeholder key booted green. These tests
exist so a guard that cannot fire is a red test, not a red demo.
"""

from __future__ import annotations

import pytest

from app.main import refuse_misconfigured_production
from app.settings import Settings


def _settings(**overrides: object) -> Settings:
    base = {"session_secret": "x" * 32, "openai_api_key": "sk-test"}
    return Settings(**{**base, **overrides})  # type: ignore[arg-type]


def test_production_refuses_the_placeholder_domain_key() -> None:
    with pytest.raises(RuntimeError, match="CHATKIT_DOMAIN_KEY"):
        refuse_misconfigured_production(
            _settings(
                public_base_url="https://uktzed.example.com",
                chatkit_domain_key="domain_pk_localhost_dev",
            )
        )


def test_production_refuses_an_empty_domain_key() -> None:
    with pytest.raises(RuntimeError, match="CHATKIT_DOMAIN_KEY"):
        refuse_misconfigured_production(
            _settings(public_base_url="https://uktzed.example.com", chatkit_domain_key="")
        )


def test_production_boots_with_a_real_looking_key() -> None:
    refuse_misconfigured_production(
        _settings(
            public_base_url="https://uktzed.example.com",
            chatkit_domain_key="domain_pk_0123456789abcdef",
        )
    )


def test_development_keeps_the_placeholder() -> None:
    """http:// is not production; the placeholder is the documented local value."""
    refuse_misconfigured_production(
        _settings(
            public_base_url="http://localhost:8000", chatkit_domain_key="domain_pk_localhost_dev"
        )
    )
