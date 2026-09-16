"""The single classifier agent — the shared payload base, the breadcrumb step, the outcome.

Three things live in this package ``__init__`` rather than in a module of their own, and all
three for the same reason: a package ``__init__`` that imports nothing but leaves is the one
import inside the package that can never cycle. ``app.agent.schemas`` needs them, and
``app.tariff.repo`` imports ``app.agent.schemas``, so anything they depend on has to sit above
that line.

* ``UAModel`` — the base every model-facing payload derives from.
* ``PathStep`` — a breadcrumb step, which is both a piece of every tool result and an argument
  type of ``emit_classification``. There must be exactly one of it.
* ``Outcome`` — v1's three-way outcome taxonomy plus the failure branch it never recorded.

``UktzedContext`` and ``Ctx`` deliberately do NOT live here; they are in
``app.agent.context``, and that module's docstring explains the cycle that put them there.
Importing them from here would reintroduce ``ImportError: cannot import name
'RequestContext' from partially initialized module 'app.context'``.

**Ukrainian JSON keys, English Python identifiers.** Every payload declares its fields in
English and carries the Ukrainian ``alias`` the model reads. The Agents SDK dumps typed tool
output with ``by_alias=True, ensure_ascii=False`` (``agents/items.py:872-883``), so the model
sees ``{"рівень":…,"коди":[…]}`` while the Python stays greppable. ``populate_by_name`` is what
lets one class serve both directions.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.tariff import TariffLevel

__all__ = [
    "Outcome",
    "PathStep",
    "UAModel",
]

Outcome = Literal["result", "clarification", "conversation", "error"]
"""v1's three-way outcome taxonomy — 1,130 / 244 / 29 of 1,403 — plus the failure it never
recorded, because v1 wrote a row only on success and every crashed turn was invisible."""


class UAModel(BaseModel):
    """Base for every model-facing payload. See the module docstring for the alias convention."""

    model_config = ConfigDict(populate_by_name=True)


class PathStep(UAModel):
    """One breadcrumb step.

    No defaults anywhere: this is an argument type of ``emit_classification`` as well as a
    piece of every tool result, and the strict-schema pass only strips ``default: null``.
    """

    level: TariffLevel = Field(alias="рівень")
    code: str = Field(alias="код")
    description: str = Field(alias="опис")
