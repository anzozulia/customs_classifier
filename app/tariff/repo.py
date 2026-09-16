"""All tariff navigation SQL, in one class.

The rule this file exists to enforce: **sections are not addressable**. All 21 section codes
are also group codes, and v1 accepted both as bare 2-digit strings and silently returned the
wrong subtree. Here a 2-digit code always means a group, the only entry point that accepts a
section code is `list_groups_in_section`, and the natural key `(dataset_id, level, code)`
makes the two rows structurally different objects.

Retrieval is the tree walk (D23). `search_candidates` is a hypothesis generator over
`full_path` trigrams, not an answer path — embeddings are milestone M4, and Postgres ships no
Ukrainian text-search configuration, so FTS is not the fallback.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from app.agent.schemas import (
    CategoryTree,
    NodeDetail,
    NodeSummary,
    PathStep,
    SectionSummary,
    TreeNode,
)
from app.tariff import TariffLevel


class Executor(Protocol):
    """What this repo needs from asyncpg: a `Connection` or a `Pool`, either will do."""

    async def fetch(self, query: str, *args: Any) -> list[Any]: ...
    async def fetchrow(self, query: str, *args: Any) -> Any: ...
    async def fetchval(self, query: str, *args: Any) -> Any: ...


class TariffError(Exception):
    """Base for data-layer lookup failures."""


class NoActiveDatasetError(TariffError):
    """No `tariff_dataset` is active. Run the ingest. `/readyz` turns this into a 503."""


class UnknownCodeError(TariffError):
    """A code that is not in the dataset was opened. The caller owns the Ukrainian message —
    it has the breadcrumb, and the breadcrumb is exactly what an error result must keep."""

    def __init__(self, code: str, level: str) -> None:
        super().__init__(f"code {code!r} (level {level!r}) is not in the active dataset")
        self.code = code
        self.level = level


@dataclass(frozen=True, slots=True)
class DatasetInfo:
    id: int
    sha256: str
    source_filename: str
    node_count: int
    terminal_count: int
    ingested_at: datetime


MAX_TREE_CHARS = 8_000
"""Adaptive cut-off for `open_category`. Measured payloads: median 591 chars, p90 2,148,
max 18,820 — so ~90% of headings come back whole and never need `expand`. Tune on traffic."""

MAX_SUBTREE_DEPTH = 3
"""Relative depth fetched under a category: 6 -> 8 -> 10 digits is the deepest real chain."""

MIN_SEARCH_SCORE = 0.15
"""Below this, trigram hits are noise. Search is a hypothesis generator, so a short list of
plausible branches beats a long list of near-zero scores."""

_LEVEL_BY_CODE_LENGTH: dict[int, TariffLevel] = {2: "group", 4: "category"}

_ACTIVE_DATASET = """
SELECT id, sha256, source_filename, node_count, terminal_count, ingested_at
FROM tariff_dataset WHERE is_active
"""

_SECTIONS = """
SELECT s.code, s.description, count(g.id) AS group_count,
       coalesce(min(g.code), '') AS first_group_code,
       coalesce(max(g.code), '') AS last_group_code
FROM tariff_node s
LEFT JOIN tariff_node g ON g.parent_id = s.id
WHERE s.dataset_id = $1 AND s.level = 'section'
GROUP BY s.id, s.code, s.description, s.sort_key
ORDER BY s.sort_key
"""

_CHILDREN = """
SELECT c.code, c.level, c.description, c.full_path, c.is_terminal
FROM tariff_node c
JOIN tariff_node p ON c.parent_id = p.id
WHERE p.dataset_id = $1 AND p.level = $2 AND p.code = $3
ORDER BY c.sort_key
"""

_NODE = """
SELECT id, code, level, description, full_path, is_terminal, depth, child_count,
       ancestor_codes, path_is_ambiguous, is_dead_end
FROM tariff_node
WHERE dataset_id = $1 AND level = $2 AND code = $3
"""

# Walk up by parent_id rather than by ancestor_codes: the codes alone cannot say which level
# they belong to, and that ambiguity is the collision this schema exists to delete.
_ANCESTRY = """
WITH RECURSIVE target AS (
    SELECT parent_id FROM tariff_node
    WHERE dataset_id = $1 AND level = $2 AND code = $3
), up AS (
    SELECT n.id, n.parent_id, n.code, n.level, n.description, n.full_path, n.is_terminal, n.depth
    FROM tariff_node n JOIN target t ON n.id = t.parent_id
  UNION ALL
    SELECT p.id, p.parent_id, p.code, p.level, p.description, p.full_path, p.is_terminal, p.depth
    FROM tariff_node p JOIN up ON up.parent_id = p.id
)
SELECT code, level, description, full_path, is_terminal FROM up ORDER BY depth
"""

_SUBTREE = """
WITH RECURSIVE sub AS (
    SELECT n.id, n.parent_id, n.code, n.description, n.is_terminal, n.child_count,
           n.sort_key, 1 AS rel_depth
    FROM tariff_node n WHERE n.parent_id = $1
  UNION ALL
    SELECT c.id, c.parent_id, c.code, c.description, c.is_terminal, c.child_count,
           c.sort_key, sub.rel_depth + 1
    FROM tariff_node c JOIN sub ON c.parent_id = sub.id
    WHERE sub.rel_depth < $2
)
SELECT id, parent_id, code, description, is_terminal, child_count
FROM sub ORDER BY sort_key
"""

# word_similarity, not similarity: the query is 2-3 words and full_path has a 666-character
# median, so whole-string trigram similarity is dominated by length and scores ~0.03 even for
# a correct hit. word_similarity scores the best matching extent instead.
#
# This is a scan of 14,187 short rows (tens of ms) and it does NOT use the trigram index: the
# index-usable operators (% and %>) test against pg_trgm's session thresholds, whose default
# 0.6 is far above anything Ukrainian tariff prose scores, and SET LOCAL on a pooled
# connection is a race waiting to happen. An explicit numeric floor is the honest version.
_SEARCH = """
SELECT code, level, description, full_path, is_terminal, score
FROM (
    SELECT code, level, description, full_path, is_terminal,
           word_similarity($2::text, full_path) AS score
    FROM tariff_node
    WHERE dataset_id = $1 AND NOT is_dead_end
) scored
WHERE score >= $3
ORDER BY score DESC, length(full_path), code
LIMIT $4
"""


def normalize_code(code: str) -> str:
    """Strip everything a human or a model might add: spaces, dots, dashes, NBSPs.

    v1 delegated the `XXXX XX XX XX` format rule to model obedience in three places. It is
    one function, applied at the boundary, once.
    """
    return "".join(ch for ch in code.strip() if ch.isdigit())


def level_for_code(code: str) -> TariffLevel:
    """2 digits is ALWAYS a group. 4 is a category. Everything else is a prefix-tree code."""
    return _LEVEL_BY_CODE_LENGTH.get(len(code), "code")


def _summary(row: Any) -> NodeSummary:
    return NodeSummary(
        code=row["code"],
        level=row["level"],
        description=row["description"],
        full_path=row["full_path"],
        is_terminal=row["is_terminal"],
    )


def _detail(row: Any) -> NodeDetail:
    return NodeDetail(
        code=row["code"],
        level=row["level"],
        description=row["description"],
        full_path=row["full_path"],
        is_terminal=row["is_terminal"],
        depth=row["depth"],
        child_count=row["child_count"],
        ancestor_codes=list(row["ancestor_codes"]),
        path_is_ambiguous=row["path_is_ambiguous"],
        is_dead_end=row["is_dead_end"],
    )


class TariffRepo:
    """Navigation over one dataset. Cheap to construct; construct one per request."""

    def __init__(self, executor: Executor, *, dataset_id: int | None = None) -> None:
        self._db = executor
        self._dataset_id = dataset_id
        self._dataset: DatasetInfo | None = None

    async def active_dataset(self) -> DatasetInfo:
        """The one active dataset. `/readyz` compares its sha256 against the file on disk."""
        if self._dataset is None:
            row = await self._db.fetchrow(_ACTIVE_DATASET)
            if row is None:
                raise NoActiveDatasetError("no active tariff_dataset row")
            self._dataset = DatasetInfo(
                id=row["id"],
                sha256=row["sha256"],
                source_filename=row["source_filename"],
                node_count=row["node_count"],
                terminal_count=row["terminal_count"],
                ingested_at=row["ingested_at"],
            )
        return self._dataset

    async def dataset_id(self) -> int:
        if self._dataset_id is None:
            self._dataset_id = (await self.active_dataset()).id
        return self._dataset_id

    # ---- navigation -------------------------------------------------------------------

    async def list_sections(self) -> list[SectionSummary]:
        """The 21 sections, with the group range each one owns.

        Section 06's groups are stored `30…38,28,29` in the source, so the range comes from
        min/max over the children and not from the order they arrive in.
        """
        rows = await self._db.fetch(_SECTIONS, await self.dataset_id())
        return [
            SectionSummary(
                code=r["code"],
                description=r["description"],
                group_count=r["group_count"],
                first_group_code=r["first_group_code"],
                last_group_code=r["last_group_code"],
            )
            for r in rows
        ]

    async def list_groups_in_section(self, section_code: str) -> list[NodeSummary]:
        """Groups of one section. The only place a section code is ever accepted.

        Group 77 is returned like any other group: the tariff has it, so the drill-down
        shows it. It is `is_dead_end` and opening it says so explicitly.
        """
        rows = await self._db.fetch(
            _CHILDREN, await self.dataset_id(), "section", normalize_code(section_code)
        )
        return [_summary(r) for r in rows]

    async def list_categories_in_group(self, group_code: str) -> list[NodeSummary]:
        """4-digit categories of one group. A 2-digit code here is a GROUP, never a section.

        Returns `[]` both for an unknown group and for the one reserved empty group
        (15/77). Callers that need to tell those apart call `resolve()` and read
        `is_dead_end` — an empty list on its own invites the model to retry.
        """
        rows = await self._db.fetch(
            _CHILDREN, await self.dataset_id(), "group", normalize_code(group_code)
        )
        return [_summary(r) for r in rows]

    async def open_category(self, category_code: str) -> CategoryTree:
        """The nested prefix tree under a 4-digit category, with its breadcrumb.

        Adaptive: the whole subtree when it serialises to at most `MAX_TREE_CHARS`, else one
        level with `collapsed` flags and `truncated=True`, and the model calls `expand`.

        Raises `UnknownCodeError` if the category is not in the dataset.
        """
        return await self._tree(normalize_code(category_code), "category")

    async def expand(self, prefix: str) -> CategoryTree:
        """The same tree, rooted at any intermediate code. The escape hatch from `truncated`.

        Not in the milestone's method list, but `open_category`'s truncated branch tells the
        model to call `expand`, so the data layer has to be able to answer it.
        """
        code = normalize_code(prefix)
        return await self._tree(code, level_for_code(code))

    async def resolve(self, code: str) -> NodeDetail | None:
        """One row, by bare code. Level is derived from the length: 2 => group, 4 =>
        category, else prefix-tree code. `None` means the code is not in the dataset."""
        normalized = normalize_code(code)
        if not normalized:
            return None
        row = await self._db.fetchrow(
            _NODE, await self.dataset_id(), level_for_code(normalized), normalized
        )
        return _detail(row) if row is not None else None

    async def ancestry(self, code: str) -> list[NodeSummary]:
        """Ancestors of a code, section first, excluding the code itself.

        Empty for a section, and empty for a code that is not in the dataset — the caller
        has already resolved it if it needs to tell those apart.
        """
        normalized = normalize_code(code)
        if not normalized:
            return []
        rows = await self._db.fetch(
            _ANCESTRY, await self.dataset_id(), level_for_code(normalized), normalized
        )
        return [_summary(r) for r in rows]

    async def search_candidates(self, query: str, limit: int = 20) -> list[NodeSummary]:
        """Trigram hypotheses over `full_path`. Never an answer — the model opens the branch.

        Consumer vocabulary is largely absent from the tariff (`ILIKE '%ноутбук%'` matches
        zero rows; it writes «машини обчислювальні портативні»), so this helps when the user
        pastes tariff-shaped language and is honestly weak otherwise. That gap is a synonymy
        problem and it closes with embeddings in M4, not with more lexical tuning.
        """
        text = " ".join(query.split())
        if not text:
            return []
        rows = await self._db.fetch(
            _SEARCH, await self.dataset_id(), text, MIN_SEARCH_SCORE, max(1, limit)
        )
        return [_summary(r) for r in rows]

    # ---- internals --------------------------------------------------------------------

    async def _tree(self, code: str, level: TariffLevel) -> CategoryTree:
        dataset_id = await self.dataset_id()
        root = await self._db.fetchrow(_NODE, dataset_id, level, code)
        if root is None:
            raise UnknownCodeError(code, level)

        rows = await self._db.fetch(_SUBTREE, root["id"], MAX_SUBTREE_DEPTH)
        ancestors = await self._db.fetch(_ANCESTRY, dataset_id, level, code)

        path = [
            PathStep(level=r["level"], code=r["code"], description=r["description"])
            for r in ancestors
        ]
        path.append(
            PathStep(level=root["level"], code=root["code"], description=root["description"])
        )

        terminal_count = sum(1 for r in rows if r["is_terminal"])
        nested = _build_forest(rows, root["id"])
        tree = CategoryTree(
            code=root["code"],
            level=root["level"],
            description=root["description"],
            full_path=root["full_path"],
            path=path,
            nodes=nested,
            terminal_count=terminal_count,
            truncated=False,
            is_dead_end=root["is_dead_end"],
        )
        # Measure what the model will actually receive: aliases are Ukrainian and longer.
        if len(tree.model_dump_json(by_alias=True)) > MAX_TREE_CHARS:
            tree = tree.model_copy(update={"nodes": _collapse(nested), "truncated": True})
        return tree


def _build_forest(rows: list[Any], root_id: int) -> list[TreeNode]:
    """Rows (already in document order) -> nested `TreeNode`s under `root_id`."""
    children: dict[int, list[Any]] = {}
    for row in rows:
        children.setdefault(row["parent_id"], []).append(row)

    def build(parent_id: int) -> list[TreeNode]:
        nodes: list[TreeNode] = []
        for row in children.get(parent_id, []):
            kids = build(row["id"])
            nodes.append(
                TreeNode(
                    code=row["code"],
                    description=row["description"],
                    is_terminal=row["is_terminal"],
                    # Children exist but are not in this payload: the depth cut reached them.
                    collapsed=row["child_count"] > 0 and not kids,
                    children=kids,
                )
            )
        return nodes

    return build(root_id)


def _collapse(nodes: list[TreeNode]) -> list[TreeNode]:
    """Keep the first level only; anything with children becomes `collapsed`."""
    return [
        node.model_copy(update={"children": [], "collapsed": bool(node.children) or node.collapsed})
        for node in nodes
    ]
