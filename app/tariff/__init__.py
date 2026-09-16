"""The tariff data layer: ingest, lookup, validation, catalogue.

This package ``__init__`` imports none of its own submodules on purpose. It holds only the
two constants both halves of the project need, so that

* ``app.tariff.ingest`` stays importable with **nothing** installed (the invariant test suite
  runs against the raw JSON with no database, no asyncpg and no Agents SDK), and
* ``app.agent.schemas`` can take the level vocabulary from here without an import cycle
  (``repo`` imports the schemas, so the schemas must not import ``repo``).
"""

from __future__ import annotations

from typing import Literal

__all__ = ["LEVELS", "PATH_SEPARATOR", "TariffLevel"]

TariffLevel = Literal["section", "group", "category", "code"]
"""The four levels.

``category`` is the 4-digit heading; ``code`` is the flat 6/8/10-digit prefix tree below it.

Sections are **not addressable**: a bare 2-digit code always means a group. All 21 section
codes are also group codes, and v1 passed both as bare 2-digit strings and silently returned
the wrong subtree. Section is derived from ``ancestor_codes[0]``, never taken as an argument.
"""

LEVELS: tuple[str, ...] = ("section", "group", "category", "code")
"""Same values as a runtime tuple, for the DDL CHECK and for argument validation."""

PATH_SEPARATOR = " > "
"""How ``full_path`` is joined at ingest. No description in the file contains it."""
