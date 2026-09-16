"""Turn-scoped provenance.

v1's measured failure mode was NOT cold hallucination (~1.3% of fresh answers) but
STALE CARRY-OVER: 36% of continued turns answered from tool output replayed in context
from an earlier turn, potentially about a different product. Nothing in v1 could tell
the two apart, and nothing forbade the second.

The fix is structural, not a prompt rule: every code the model sees is recorded here
against the tool call that produced it IN THIS TURN, and the terminal tool refuses any
code that is not in this turn's candidate set.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


class ProvenanceError(Exception):
    """A code was emitted that this turn's tool results never contained."""


@dataclass(frozen=True, slots=True)
class CodeEvidence:
    """One code, and the tool call that put it in front of the model this turn."""

    code: str
    description: str
    full_path: str
    is_terminal: bool
    tool_call_index: int


@dataclass(slots=True)
class ToolCallRecord:
    index: int
    tool_name: str
    args: dict
    started_at: float
    duration_ms: int | None = None
    result_digest: str | None = None
    error: str | None = None


@dataclass
class TurnLedger:
    """Reset at the start of every turn. Never persists across turns — that is the point."""

    calls: list[ToolCallRecord] = field(default_factory=list)
    _seen: dict[str, CodeEvidence] = field(default_factory=dict)

    def reset(self) -> None:
        self.calls.clear()
        self._seen.clear()

    def begin_call(self, tool_name: str, args: dict) -> ToolCallRecord:
        rec = ToolCallRecord(
            index=len(self.calls), tool_name=tool_name, args=args, started_at=time.monotonic()
        )
        self.calls.append(rec)
        return rec

    def end_call(
        self, rec: ToolCallRecord, *, digest: str | None = None, error: str | None = None
    ) -> None:
        rec.duration_ms = int((time.monotonic() - rec.started_at) * 1000)
        rec.result_digest = digest
        rec.error = error

    def record_codes(self, evidence: list[CodeEvidence]) -> None:
        """Called by every navigation tool with everything it showed the model."""
        for e in evidence:
            # First sighting wins: it carries the tool call that actually surfaced it.
            self._seen.setdefault(e.code, e)

    def evidence_for(self, code: str) -> CodeEvidence | None:
        return self._seen.get(code)

    def require(self, code: str) -> CodeEvidence:
        """The enforcement point. Raises unless this turn surfaced the code."""
        ev = self._seen.get(code)
        if ev is None:
            raise ProvenanceError(
                f"code {code!r} was not returned by any tool call in this turn "
                f"({len(self._seen)} codes seen across {len(self.calls)} calls)"
            )
        return ev

    @property
    def codes_seen(self) -> int:
        return len(self._seen)
