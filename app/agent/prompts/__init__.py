"""Prompt rendering.

`PROMPT_VERSION` is stamped on every classification row together with the sha256 of the
*rendered* text, so "which prompt produced this answer" is answerable from SQL alone. v1
could not answer it at all: four documents gave four different accounts of what it ran.

**No Jinja2.** The template is named `.jinja` because the architecture names it so and
because editors highlight it, but it has exactly two holes and jinja2 is not a dependency of
this project. A `{{ name }}` substitution plus a `{# … #}` comment strip is twenty lines;
adding a templating engine for two variables is not. Unfilled holes raise rather than ship a
literal `{{ … }}` to the model — that is the one Jinja behaviour worth keeping.
"""

from __future__ import annotations

import hashlib
import re
from functools import lru_cache
from pathlib import Path

__all__ = [
    "PROMPT_VERSION",
    "TEMPLATE_SHA256",
    "render_system_prompt",
]

PROMPT_VERSION = "p3.2026-09-17"
"""Bump on every edit to anything in this package. `tests/test_prompt_version.py` enforces it
against `TEMPLATE_SHA256`."""

_TEMPLATE_PATH = Path(__file__).with_name("classifier_system.jinja")
_COMMENT_RE = re.compile(r"\{#.*?#\}", re.DOTALL)
_HOLE_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")


@lru_cache(maxsize=1)
def _template() -> str:
    return _TEMPLATE_PATH.read_text(encoding="utf-8")


TEMPLATE_SHA256 = hashlib.sha256(_TEMPLATE_PATH.read_bytes()).hexdigest()
"""Hash of the template file on disk, comments included."""


class PromptRenderError(RuntimeError):
    """A hole in the template was left unfilled. Never ship `{{ … }}` to a model."""


def render_system_prompt(*, catalogue: str, locale: str = "uk") -> tuple[str, str]:
    """Render P1.

    Args:
        catalogue: P2, the section catalogue, generated at ingest and frozen with the dataset.
        locale: the request locale, threaded through instead of asserting "answer in
            Ukrainian" seven times the way v1 did.

    Returns:
        `(text, sha256)` — the exact string handed to the model and the hash recorded with
        the turn.
    """
    values = {"sections_catalogue": catalogue.strip(), "locale": locale}
    body = _COMMENT_RE.sub("", _template()).strip()

    missing: list[str] = []

    def _fill(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values:
            missing.append(key)
            return match.group(0)
        return values[key]

    text = _HOLE_RE.sub(_fill, body)
    if missing:
        raise PromptRenderError(f"unfilled template holes: {sorted(set(missing))}")
    # Collapse the blank line a stripped comment block leaves behind, so the cached prefix is
    # byte-stable regardless of how the comments are laid out.
    text = re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"
    return text, hashlib.sha256(text.encode("utf-8")).hexdigest()
