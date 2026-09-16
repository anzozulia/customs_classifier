"""Per-request context.

This object IS the multi-user boundary. `chatkit.Store` and `ChatKitServer` are both
Generic[TContext] and every Store method receives this — so scoping is structural rather
than something each query has to remember. ChatKitServer itself performs NO authorization.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.agent.provenance import TurnLedger


@dataclass
class RequestContext:
    """Passed to ChatKitServer.process() and handed to every Store method."""

    user_id: int
    request_id: str
    locale: str = "uk"
    # Turn-scoped provenance. A code may only be emitted if THIS turn's tool results
    # contained it — the structural fix for v1's measured 36% stale-carry-over rate.
    ledger: TurnLedger = field(default_factory=TurnLedger)
