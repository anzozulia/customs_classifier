"""The per-run agent context — and the one file that keeps the import graph acyclic.

`UktzedContext` lives here rather than in `app/agent/__init__.py` (the architecture's §5.4
location, and the one that actually works) because of a hard cycle:

    app/context.py          -> app.agent.provenance   (needs TurnLedger at runtime,
                                                        for `field(default_factory=…)`)
    importing app.agent.provenance  -> runs app/agent/__init__.py

so a module-level `from app.context import RequestContext` inside `app/agent/__init__.py`
deadlocks whenever `app.context` is imported FIRST:

    ImportError: cannot import name 'RequestContext' from partially initialized module
                 'app.context' (most likely due to a circular import)

It looked intermittent because importing `app.agent` first happens to work — but
`app.chat.routes`, `app.chat.store` and therefore `app.main` all reach `app.context` first,
and `app.main` is the ASGI entry point. `RequestContext` is needed at class-creation time
(`AgentContext[RequestContext]` is a runtime subscript), so `if TYPE_CHECKING` cannot fix it;
the import has to leave the package `__init__` altogether.

What remains in `app/agent/__init__.py` — `UAModel`, `PathStep`, `Outcome` — depends on
nothing but pydantic and `app.tariff`, so that `__init__` is now genuinely cycle-free.
"""

from __future__ import annotations

from typing import Any

from agents import RunContextWrapper
from chatkit.agents import AgentContext
from pydantic import Field

from app.agent import Outcome
from app.agent.provenance import TurnLedger
from app.context import RequestContext

__all__ = ["Ctx", "UktzedContext"]


class UktzedContext(AgentContext[RequestContext]):
    """``AgentContext`` plus the per-run state the agent layer needs.

    ``tariff`` is typed ``Any`` rather than ``TariffRepo``, and that is a cycle, not laziness:
    ``app.tariff.repo`` imports ``app.agent.schemas``, which imports ``app.agent``. Declaring
    the real class here would close the loop. The seam is documented instead of abstracted —
    the methods used are exactly ``TariffRepo``'s, and ``tools_nav`` names them.
    """

    tariff: Any = None
    repairs: int = 0
    outcome: Outcome = "conversation"
    emitted_codes: list[str] = Field(default_factory=list)

    @property
    def ledger(self) -> TurnLedger:
        """The turn-scoped provenance ledger. One HTTP request == one turn == one ledger."""
        return self.request_context.ledger


Ctx = RunContextWrapper[UktzedContext]
"""The first parameter of every function tool. ``get_type_hints`` resolves the alias and
``get_origin(Ctx) is RunContextWrapper`` is what the SDK checks (``function_schema.py:402``)."""
