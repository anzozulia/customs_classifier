"""localhost and 127.0.0.1 are the same machine and different origins.

Reported from the browser: in PUBLIC mode, opening the app and sending a message bounced the
visitor to a login page that told them «входити не потрібно» — a screen with nothing to do on
it and no way forward.

Two bugs met:
  1. `allowed_origins()` contained exactly PUBLIC_BASE_URL, so browsing 127.0.0.1 while the
     app was configured as localhost made every POST a 403.
  2. The SPA treated 403 like 401 and redirected to /login.

This file pins the server half.
"""

from __future__ import annotations

import pytest

from app.auth.deps import allowed_origins
from app.settings import get_settings


@pytest.fixture(autouse=True)
def _clear_caches():
    get_settings.cache_clear()
    allowed_origins.cache_clear()
    yield
    get_settings.cache_clear()
    allowed_origins.cache_clear()


def _origins_for(base_url: str, monkeypatch: pytest.MonkeyPatch) -> frozenset[str]:
    monkeypatch.setenv("PUBLIC_BASE_URL", base_url)
    get_settings.cache_clear()
    allowed_origins.cache_clear()
    return allowed_origins()


@pytest.mark.parametrize("configured", ["http://localhost:8000", "http://127.0.0.1:8000"])
def test_loopback_aliases_are_one_origin(
    configured: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE regression: whichever loopback spelling is configured, the others still work."""
    origins = _origins_for(configured, monkeypatch)
    assert "http://localhost:8000" in origins
    assert "http://127.0.0.1:8000" in origins
    assert "http://[::1]:8000" in origins


def test_the_port_is_part_of_the_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    """A different port is a different origin, loopback or not — no relaxation there."""
    origins = _origins_for("http://localhost:8000", monkeypatch)
    assert "http://localhost:9999" not in origins
    assert "http://127.0.0.1:9999" not in origins


def test_the_scheme_is_part_of_the_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    origins = _origins_for("https://localhost", monkeypatch)
    assert "https://localhost" in origins
    assert "http://localhost" not in origins


def test_a_real_deployment_gets_exactly_one_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    """The relaxation is loopback-only. Nothing is widened for a real host."""
    origins = _origins_for("https://uktzed.example.com", monkeypatch)
    assert origins == frozenset({"https://uktzed.example.com"})


def test_a_real_deployment_does_not_admit_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    origins = _origins_for("https://uktzed.example.com", monkeypatch)
    assert "http://localhost" not in origins
    assert "https://127.0.0.1" not in origins
