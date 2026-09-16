# ruff: noqa: RUF001, RUF002  -- Ukrainian text below. Single-letter Cyrillic words and
# the en dash are correct typography here, not Latin look-alikes; pyproject pins ruff's
# defaults (no allowed-confusables), so the exemption is declared per file.
"""P2 — the `{sections_catalogue}` block injected into the system prompt.

Generated from the database, frozen with the dataset, hashed into every classification row.
Five rules, each one deleting prompt text that v1 needed because its catalogue was ambiguous:

1. Sort by code. Section 06's groups are stored `30…38,28,29`; a naive builder emits
   "(групи 30–29)".
2. Label both numbering systems — `Розділ NN (групи A–B):` — instead of leaving the model to
   infer which 2-digit namespace a number belongs to. v1 needed 622 characters of prose for
   exactly this.
3. Expand the group titles under any section whose description is not unique or that owns at
   most three groups. That is what gives sections 19 and 20 any signal: both are literally
   "Різні промислові товари".
4. Skip placeholder group titles matching `^Група \\d\\d$` — group 77, reserved and empty.
5. No group-range arithmetic anywhere in the prompt: the model never derives a section from a
   group, because it is never asked for one.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter

from app.tariff.repo import TariffRepo

_PLACEHOLDER_TITLE = re.compile(r"^Група \d\d$")
_EXPAND_AT_MOST = 3
_INDENT = "    "


async def build_sections_catalogue(repo: TariffRepo) -> str:
    """Render the catalogue for the active dataset."""
    sections = await repo.list_sections()
    descriptions = Counter(s.description for s in sections)

    lines: list[str] = []
    for section in sections:
        lines.append(
            f"Розділ {section.code} "
            f"(групи {section.first_group_code}–{section.last_group_code}): "
            f"{section.description}"
        )
        if descriptions[section.description] > 1 or section.group_count <= _EXPAND_AT_MOST:
            for group in await repo.list_groups_in_section(section.code):
                if _PLACEHOLDER_TITLE.match(group.description):
                    continue
                lines.append(f"{_INDENT}{group.code} — {group.description}")
    return "\n".join(lines)


def catalogue_digest(catalogue: str) -> str:
    """sha256 of the rendered catalogue. Pinned into the prompt version and the records."""
    return hashlib.sha256(catalogue.encode("utf-8")).hexdigest()
