"""The hidden superuser panel's HTTP API.

One router, mounted at `/api/admin` by `app/main.py`, entirely behind
`app.auth.deps.require_superuser` — which answers 404, never 403, so the panel is hidden
rather than merely protected.
"""

from __future__ import annotations

from app.admin.routes import router

__all__ = ["router"]
