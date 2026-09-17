"""JSON -> `tariff_node`, idempotent per content sha256.

The source file gives three real levels of nesting (section > group > category) and then a
FLAT list that mixes 6-, 8- and 10-digit codes. The real structure of that list is by string
prefix, and it is incomplete: 3,236 ten-digit codes have no 6-digit parent in the file.

Two rules follow, and they are the whole reason this module exists:

* **Parent = longest EXISTING prefix among siblings**, else the 4-digit category. We do not
  synthesise the missing 6/8-digit rows. Inventing them would put text that is not in the
  tariff into `full_path` — and `full_path` is the only thing anything downstream reads.
* **`is_terminal` is authored here**: "no other code in this category has me as a strict
  prefix". On this snapshot it agrees with `len(code) == 10` on all 10,490 rows; that is a
  property of the snapshot, not of the nomenclature, so we never spell it `len(code) == 10`.

Invariants fail the load. They are a `DatasetShape` parameter rather than constants because a
genuine tariff update *should* trip them: a new release then becomes a deliberate, reviewed
ingest instead of a silent swap.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.tariff import PATH_SEPARATOR, TariffLevel

if TYPE_CHECKING:  # asyncpg is a runtime dep of the app, but not of the pure functions here,
    import asyncpg  # so the invariant test suite runs with nothing installed at all.


class IngestError(Exception):
    """Ingest refused the file."""


class IngestInvariantError(IngestError):
    """One or more dataset invariants failed. Every failure is listed, not just the first."""

    def __init__(self, failures: list[str]) -> None:
        super().__init__("dataset invariants failed:\n  - " + "\n  - ".join(failures))
        self.failures = failures


@dataclass(frozen=True, slots=True)
class DatasetShape:
    """What we expect this exact tariff snapshot to contain. Measured, not guessed."""

    node_count: int = 14_187
    terminal_count: int = 10_490
    section_count: int = 21
    group_count: int = 97
    category_count: int = 957
    # depth = len(ancestor_codes): section 0, group 1, category 2, prefix-tree nodes 3..5.
    max_depth: int = 5
    # Exactly one: section 15 / group 77 ("Група 77"), reserved and childless.
    dead_end_count: int = 1
    # Colon cross-check: 2,622/2,622 internal nodes end with ':' but only 6 of 10,490 leaves do.
    max_terminal_colon: int = 6


EXPECTED = DatasetShape()

SOURCE_SHA256 = "5a113fc09ac05028afbd6ed1884a95133b65e7827e61a87d74d5ffb11a38dee3"
"""sha256 of the committed `data/uktzed_hierarchical.json`. `/readyz` compares against it."""


@dataclass(slots=True)
class IngestNode:
    """One row-to-be. `raw_description` is not persisted — see `COPY_COLUMNS`."""

    code: str
    level: TariffLevel
    depth: int
    raw_description: str
    description: str
    full_path: str
    ancestor_codes: list[str]
    is_terminal: bool
    child_count: int
    sort_key: str
    parent_key: tuple[str, str] | None  # (level, code) of the parent, None for sections
    path_is_ambiguous: bool = False
    is_dead_end: bool = False

    @property
    def key(self) -> tuple[str, str]:
        return (self.level, self.code)


def clean(text: str) -> str:
    """Collapse whitespace and drop the trailing ':' that marks a non-terminal heading.

    The ':' is typographic, not legal text, so we store only the cleaned form and keep the
    raw string in memory just long enough for the colon cross-check.
    """
    return " ".join(text.split()).rstrip(":").strip()


def _join(prefix: str, description: str) -> str:
    return f"{prefix}{PATH_SEPARATOR}{description}" if prefix else description


def _flatten_prefix_tree(
    category: dict[str, Any], category_path: str, category_ancestors: list[str]
) -> list[IngestNode]:
    """Rebuild the nesting of one category's flat child list from string prefixes.

    `category_ancestors` is the category's OWN ancestor list ([section, group]); the seeded
    entries below make every child's ancestors `[section, group, category, …]`.
    """
    children: list[dict[str, Any]] = category.get("children") or []
    codes = {c["code"] for c in children}
    raw = {c["code"]: c["description"] for c in children}
    c_code = category["code"]

    parent_of: dict[str, str] = {}
    for code in codes:
        prefixes = [other for other in codes if other != code and code.startswith(other)]
        parent_of[code] = max(prefixes, key=len) if prefixes else c_code

    internal = set(parent_of.values()) - {c_code}
    child_count = dict.fromkeys(codes, 0)
    for parent in parent_of.values():
        if parent in child_count:
            child_count[parent] += 1

    paths = {c_code: category_path}
    ancestors = {c_code: category_ancestors}
    nodes: list[IngestNode] = []

    # Shortest code first, so every parent's path exists before a child needs it.
    for code in sorted(codes, key=lambda c: (len(c), c)):
        parent = parent_of[code]
        description = clean(raw[code])
        paths[code] = _join(paths[parent], description)
        ancestors[code] = [*ancestors[parent], parent]
        nodes.append(
            IngestNode(
                code=code,
                level="code",
                depth=len(ancestors[code]),
                raw_description=raw[code],
                description=description,
                full_path=paths[code],
                ancestor_codes=ancestors[code],
                is_terminal=code not in internal,
                child_count=child_count[code],
                sort_key="/".join([*ancestors[code], code]),
                parent_key=("category", parent) if parent == c_code else ("code", parent),
            )
        )
    return nodes


def build_nodes(sections: list[dict[str, Any]]) -> list[IngestNode]:
    """Flatten the whole file into rows, parents before children."""
    nodes: list[IngestNode] = []

    for section in sections:
        s_code = section["code"]
        s_desc = clean(section["description"])
        groups: list[dict[str, Any]] = section.get("children") or []
        nodes.append(
            IngestNode(
                code=s_code,
                level="section",
                depth=0,
                raw_description=section["description"],
                description=s_desc,
                full_path=s_desc,
                ancestor_codes=[],
                is_terminal=False,
                child_count=len(groups),
                sort_key=s_code,
                parent_key=None,
            )
        )

        for group in groups:
            g_code = group["code"]
            g_desc = clean(group["description"])
            g_path = _join(s_desc, g_desc)
            # Group 77 has no "children" key at all. `or []` is the whole fix — the anomaly
            # surfaces later as is_dead_end, which the tools turn into an explicit
            # "зарезервована" message rather than an empty list that invites a retry.
            categories: list[dict[str, Any]] = group.get("children") or []
            nodes.append(
                IngestNode(
                    code=g_code,
                    level="group",
                    depth=1,
                    raw_description=group["description"],
                    description=g_desc,
                    full_path=g_path,
                    ancestor_codes=[s_code],
                    is_terminal=False,
                    child_count=len(categories),
                    sort_key=f"{s_code}/{g_code}",
                    parent_key=("section", s_code),
                )
            )

            for category in categories:
                c_code = category["code"]
                c_desc = clean(category["description"])
                c_path = _join(g_path, c_desc)
                c_ancestors = [s_code, g_code]
                leaves = category.get("children") or []
                subtree = _flatten_prefix_tree(category, c_path, c_ancestors)
                nodes.append(
                    IngestNode(
                        code=c_code,
                        level="category",
                        depth=2,
                        raw_description=category["description"],
                        description=c_desc,
                        full_path=c_path,
                        ancestor_codes=c_ancestors,
                        is_terminal=not leaves,
                        child_count=sum(1 for n in subtree if n.parent_key == ("category", c_code)),
                        sort_key=f"{s_code}/{g_code}/{c_code}",
                        parent_key=("group", g_code),
                    )
                )
                nodes.extend(subtree)

    mark_anomalies(nodes)
    return nodes


def mark_anomalies(nodes: list[IngestNode]) -> None:
    """Two flags the tools need, both derived, both cheap."""
    path_counts = Counter(n.full_path for n in nodes if n.is_terminal)
    for node in nodes:
        # 2,487 terminals (23.71%) share a full_path with a sibling — the dataset cannot
        # separate them by text, so the tools must show the whole set.
        node.path_is_ambiguous = node.is_terminal and path_counts[node.full_path] > 1
        # Not terminal and no children: reserved by construction. Exactly one row.
        node.is_dead_end = not node.is_terminal and node.child_count == 0


def check_invariants(nodes: list[IngestNode], shape: DatasetShape = EXPECTED) -> None:
    """Assert everything worth asserting, and raise with the full list of what broke."""
    levels = Counter(n.level for n in nodes)
    terminals = [n for n in nodes if n.is_terminal]
    failures: list[str] = []

    def want(label: str, got: object, expected: object) -> None:
        if got != expected:
            failures.append(f"{label}: got {got!r}, expected {expected!r}")

    want("node count", len(nodes), shape.node_count)
    want("terminal count", len(terminals), shape.terminal_count)
    want("section count", levels["section"], shape.section_count)
    want("group count", levels["group"], shape.group_count)
    want("category count", levels["category"], shape.category_count)
    want("max depth", max((n.depth for n in nodes), default=-1), shape.max_depth)
    want("dead-end count", sum(1 for n in nodes if n.is_dead_end), shape.dead_end_count)

    bad_len = [n.code for n in terminals if len(n.code) != 10]
    if bad_len:
        failures.append(f"{len(bad_len)} terminal(s) are not 10 digits, e.g. {bad_len[:5]}")

    duplicates = [k for k, c in Counter(n.key for n in nodes).items() if c > 1]
    if duplicates:
        failures.append(f"{len(duplicates)} duplicate (level, code) key(s), e.g. {duplicates[:5]}")

    empty_paths = [n.code for n in nodes if not n.full_path.strip()]
    if empty_paths:
        failures.append(f"{len(empty_paths)} empty full_path(s), e.g. {empty_paths[:5]}")

    broken_prefix = [
        n.code for n in nodes if n.level == "code" and not n.code.startswith(n.ancestor_codes[-1])
    ]
    if broken_prefix:
        failures.append(f"{len(broken_prefix)} code(s) do not extend their parent's code")

    leafless = [n.code for n in terminals if n.child_count != 0]
    if leafless:
        failures.append(f"{len(leafless)} terminal(s) have children")

    orphans = [n.code for n in nodes if n.parent_key is None and n.level != "section"]
    if orphans:
        failures.append(f"{len(orphans)} non-section node(s) without a parent")

    colon = sum(1 for n in terminals if n.raw_description.rstrip().endswith(":"))
    if colon > shape.max_terminal_colon:
        failures.append(
            f"colon cross-check: {colon} terminals end with ':', "
            f"expected at most {shape.max_terminal_colon}"
        )

    if failures:
        raise IngestInvariantError(failures)


def load_source(path: Path) -> tuple[str, list[dict[str, Any]]]:
    """Return (sha256 of the raw bytes, parsed sections)."""
    raw = path.read_bytes()
    return hashlib.sha256(raw).hexdigest(), json.loads(raw)


def prepare(path: Path, shape: DatasetShape = EXPECTED) -> tuple[str, list[IngestNode]]:
    """Parse + flatten + validate. No database involved — this is what the tests exercise."""
    digest, sections = load_source(path)
    nodes = build_nodes(sections)
    check_invariants(nodes, shape)
    return digest, nodes


@dataclass(frozen=True, slots=True)
class IngestResult:
    dataset_id: int
    sha256: str
    node_count: int
    terminal_count: int
    created: bool
    activated: bool


COPY_COLUMNS = (
    "dataset_id",
    "code",
    "level",
    "depth",
    "description",
    "full_path",
    "ancestor_codes",
    "is_terminal",
    "child_count",
    "path_is_ambiguous",
    "is_dead_end",
    "sort_key",
)

_WIRE_PARENTS = """
UPDATE tariff_node AS child
   SET parent_id = wiring.parent_id
  FROM unnest($1::bigint[], $2::bigint[]) AS wiring(id, parent_id)
 WHERE child.id = wiring.id
"""


async def ingest_file(
    conn: asyncpg.Connection,
    path: Path,
    *,
    activate: bool = True,
    shape: DatasetShape = EXPECTED,
) -> IngestResult:
    """Load `path` into a new `tariff_dataset` + its `tariff_node` rows.

    Idempotent per content sha256: re-running the same file inserts nothing and returns the
    existing dataset id. Needs a `Connection`, not a `Pool` — `copy_records_to_table` is a
    connection-level method.
    """
    digest, nodes = prepare(path, shape)
    terminal_count = sum(1 for n in nodes if n.is_terminal)

    existing = await conn.fetchrow(
        "SELECT id, node_count, terminal_count, is_active FROM tariff_dataset WHERE sha256 = $1",
        digest,
    )
    if existing is not None:
        activated = False
        if activate and not existing["is_active"]:
            async with conn.transaction():
                await _activate(conn, existing["id"])
            activated = True
        return IngestResult(
            dataset_id=existing["id"],
            sha256=digest,
            node_count=existing["node_count"],
            terminal_count=existing["terminal_count"],
            created=False,
            activated=activated,
        )

    async with conn.transaction():
        dataset_id: int = await conn.fetchval(
            "INSERT INTO tariff_dataset (source_filename, sha256, node_count, terminal_count) "
            "VALUES ($1, $2, $3, $4) RETURNING id",
            path.name,
            digest,
            len(nodes),
            terminal_count,
        )

        # Pass 1: every row, parent_id still NULL.
        await conn.copy_records_to_table(
            "tariff_node",
            columns=list(COPY_COLUMNS),
            records=(
                (
                    dataset_id,
                    n.code,
                    n.level,
                    n.depth,
                    n.description,
                    n.full_path,
                    n.ancestor_codes,
                    n.is_terminal,
                    n.child_count,
                    n.path_is_ambiguous,
                    n.is_dead_end,
                    n.sort_key,
                )
                for n in nodes
            ),
        )

        # Pass 2: read the generated ids back, keyed by the natural key.
        ids = {
            (r["level"], r["code"]): r["id"]
            for r in await conn.fetch(
                "SELECT id, level, code FROM tariff_node WHERE dataset_id = $1", dataset_id
            )
        }

        # Pass 3: one UPDATE wires the whole tree. 14,187 rows, sub-second. Nothing cleverer.
        child_ids: list[int] = []
        parent_ids: list[int] = []
        for node in nodes:
            if node.parent_key is None:
                continue
            child_ids.append(ids[node.key])
            parent_ids.append(ids[node.parent_key])
        await conn.execute(_WIRE_PARENTS, child_ids, parent_ids)

        if activate:
            await _activate(conn, dataset_id)

    return IngestResult(
        dataset_id=dataset_id,
        sha256=digest,
        node_count=len(nodes),
        terminal_count=terminal_count,
        created=True,
        activated=activate,
    )


async def _activate(conn: asyncpg.Connection, dataset_id: int) -> None:
    """Exactly one active dataset; the partial unique index enforces it, this sets it."""
    await conn.execute("UPDATE tariff_dataset SET is_active = FALSE WHERE is_active")
    await conn.execute("UPDATE tariff_dataset SET is_active = TRUE WHERE id = $1", dataset_id)
