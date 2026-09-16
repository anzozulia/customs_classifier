"""Model-facing payloads for the tariff tools.

English Python identifiers, Ukrainian JSON keys — the convention `app/agent/__init__.py`
sets out: every field carries the alias the model actually reads, and the Agents SDK dumps
typed tool output `by_alias=True, ensure_ascii=False`. So the repo constructs
`NodeSummary(code=…, full_path=…)` and the model reads `{"код":…,"повний_шлях":…}`.

`PathStep` is re-exported from `app.agent` rather than redefined: it is an argument type of
`emit_classification` as well as a piece of every tool result, and there must be exactly one.

One constraint on the whole module: **no non-None defaults**. `PathStep` and `Alternative`
are terminal-tool *arguments*, and the strict-schema pass only strips `default: null`
(`agents/strict_schema.py`), so a `default: false` would survive into a strict schema.
"""

from __future__ import annotations

from collections.abc import Iterator

from pydantic import Field

from app.agent import PathStep, UAModel
from app.tariff import PATH_SEPARATOR, TariffLevel

__all__ = [
    "Ack",
    "Alternative",
    "CandidateList",
    "CategoryTree",
    "NodeDetail",
    "NodeSummary",
    "PathStep",
    "SectionSummary",
    "TreeNode",
    "iter_tree_paths",
]


class NodeSummary(UAModel):
    """One row, as the model sees it.

    `full_path` is mandatory and never reconstructed by the model: 2,473 of the 10,490 leaf
    descriptions are literally "інші", and 46% are 15 characters or shorter.
    """

    code: str = Field(alias="код")
    level: TariffLevel = Field(alias="рівень")
    description: str = Field(alias="опис")
    full_path: str = Field(alias="повний_шлях")
    is_terminal: bool = Field(alias="термінальний")


class SectionSummary(UAModel):
    """A section and the group range it owns — both numbering systems, both labelled."""

    code: str = Field(alias="код")
    description: str = Field(alias="опис")
    group_count: int = Field(alias="кількість_груп")
    first_group_code: str = Field(alias="перша_група")
    last_group_code: str = Field(alias="остання_група")


class NodeDetail(UAModel):
    """Everything `resolve()` knows about one code. The validation gate reads this."""

    code: str = Field(alias="код")
    level: TariffLevel = Field(alias="рівень")
    description: str = Field(alias="опис")
    full_path: str = Field(alias="повний_шлях")
    is_terminal: bool = Field(alias="термінальний")
    depth: int = Field(alias="глибина")
    child_count: int = Field(alias="кількість_нащадків")
    ancestor_codes: list[str] = Field(alias="коди_предків")
    path_is_ambiguous: bool = Field(alias="неоднозначний_шлях")
    is_dead_end: bool = Field(alias="зарезервовано")

    @property
    def section_code(self) -> str:
        """Derived, never supplied: `ancestor_codes[0]` is always the section."""
        return self.ancestor_codes[0] if self.ancestor_codes else self.code


class TreeNode(UAModel):
    """A node of the nested prefix tree.

    Recursive through a `$ref`, which the strict-schema pass leaves intact (a bare `$ref`
    has no incompatible siblings to unravel).

    `collapsed` means "this node has children that are not in this payload" — the model has
    to call `expand`. There is deliberately no `full_path` here: it would multiply the
    payload by the depth of the tree, and `iter_tree_paths()` rebuilds it code-side.
    """

    code: str = Field(alias="код")
    description: str = Field(alias="опис")
    is_terminal: bool = Field(alias="термінальний")
    collapsed: bool = Field(alias="згорнуто")
    children: list[TreeNode] = Field(alias="нащадки")


class CategoryTree(UAModel):
    """The drill-down payload: a breadcrumb, plus the nested subtree under one node.

    `path` ends with the node itself, so the model can print the breadcrumb verbatim.
    `terminal_count` counts the whole subtree, including anything `truncated` hid.
    """

    code: str = Field(alias="код")
    level: TariffLevel = Field(alias="рівень")
    description: str = Field(alias="опис")
    full_path: str = Field(alias="повний_шлях")
    path: list[PathStep] = Field(alias="шлях")
    nodes: list[TreeNode] = Field(alias="коди")
    terminal_count: int = Field(alias="кінцевих_кодів")
    truncated: bool = Field(alias="згорнуто")
    is_dead_end: bool = Field(alias="зарезервовано")


class CandidateList(UAModel):
    """Search output. Hypotheses, not an answer — the model still has to open the branch."""

    query: str = Field(alias="запит")
    candidates: list[NodeSummary] = Field(alias="гіпотези")


class Alternative(UAModel):
    """A code that was seriously considered and rejected, with the reason it was rejected."""

    code: str = Field(alias="код")
    description: str = Field(alias="опис")
    reason: str = Field(alias="причина")


class Ack(UAModel):
    """What a terminal tool returns.

    `ok=False` keeps the run alive so the model can correct itself — which is why a rejected
    code is a field here and not an exception (§6.4, `ToolsToFinalOutputFunction`).
    """

    ok: bool = Field(alias="успіх")
    error_code: str | None = Field(default=None, alias="код_помилки")
    message: str | None = Field(default=None, alias="повідомлення")


def iter_tree_paths(tree: CategoryTree) -> Iterator[tuple[TreeNode, str]]:
    """Yield every node of a `CategoryTree` with its reconstructed `full_path`.

    Code-side only: this is how `TurnLedger.record_codes` learns the full path of everything
    the model was just shown, without paying for `full_path` on every node of the payload.
    The reconstruction is the same concatenation ingest performed, so the strings match the
    stored ones exactly.
    """

    def walk(nodes: list[TreeNode], prefix: str) -> Iterator[tuple[TreeNode, str]]:
        for node in nodes:
            path = f"{prefix}{PATH_SEPARATOR}{node.description}"
            yield node, path
            if node.children:
                yield from walk(node.children, path)

    yield from walk(tree.nodes, tree.full_path)
