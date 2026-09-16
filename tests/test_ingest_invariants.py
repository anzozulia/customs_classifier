
# source file; the Cyrillic letters that ruff reads as Latin look-alikes are correct.
"""Invariants of the tariff snapshot, checked against the real JSON.

No database, no asyncpg, no Agents SDK, no network: this suite parses
`data/uktzed_hierarchical.json` and asserts what ingest is supposed to produce. It is the
cheapest possible guard on the one input the whole product is a wrapper around — and v1
`.gitignore`d that file, so nothing could have been asserted about it at all.

Every count here is measured, and every count is meant to break on a genuine tariff update.
That is the design: a new release becomes a deliberate, reviewed ingest instead of a silent
swap. When it breaks, re-measure and update `DatasetShape`.

Runnable two ways:  `pytest tests/test_ingest_invariants.py`  or  `python3 tests/…` for the
numbers on stdout.
"""

from __future__ import annotations

import sys
from collections import Counter
from functools import lru_cache
from pathlib import Path

# The project is a plain source tree (no [build-system], nothing installed), so make the
# repository root importable regardless of how this file is invoked.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.tariff.ingest import (  # noqa: E402
    EXPECTED,
    SOURCE_SHA256,
    IngestInvariantError,
    IngestNode,
    build_nodes,
    check_invariants,
    load_source,
    prepare,
)

SOURCE = ROOT / "data" / "uktzed_hierarchical.json"

# 3919101200, the worked example from v1's prompt. v1 advertised its description as
# "…самоклейні, у рулонах завширшки не більш як 20 см, з полівінілхлориду або поліетилену",
# which is NOT what the file says: the heading ends "у рулонах або не у рулонах", and
# "у рулонах завширшки не більш як 20 см" is the 6-digit subheading below it. The test
# asserts the data, not the prompt.
EXAMPLE_CODE = "3919101200"
EXAMPLE_PATH_PARTS = (
    "Полімерні матеріали, пластмаси та вироби з них; каучук, гума та вироби з них",
    "Пластмаси, полімерні матеріали та вироби з них",
    "Плити, листи, смужки, стрічки, плівки та інші плоскі форми з пластмаси самоклейні, "
    "у рулонах або не у рулонах",
    "у рулонах завширшки не більш як 20 см",
    "з полівінілхлориду або поліетилену",
)


@lru_cache(maxsize=1)
def nodes() -> tuple[IngestNode, ...]:
    """Parsed once; `prepare` runs `check_invariants` itself, so importing is already a test."""
    _, parsed = prepare(SOURCE)
    return tuple(parsed)


def by_code() -> dict[tuple[str, str], IngestNode]:
    return {n.key: n for n in nodes()}


def test_source_file_is_the_pinned_snapshot() -> None:
    digest, _ = load_source(SOURCE)
    assert digest == SOURCE_SHA256


def test_level_counts() -> None:
    levels = Counter(n.level for n in nodes())
    assert len(nodes()) == EXPECTED.node_count == 14_187
    assert levels["section"] == 21
    assert levels["group"] == 97
    assert levels["category"] == 957
    assert levels["code"] == 13_112


def test_terminal_count_and_shape() -> None:
    terminals = [n for n in nodes() if n.is_terminal]
    assert len(terminals) == EXPECTED.terminal_count == 10_490
    assert all(len(n.code) == 10 for n in terminals)
    assert all(n.child_count == 0 for n in terminals)


def test_is_terminal_is_authored_not_inferred() -> None:
    """On THIS snapshot the authored flag and `len(code) == 10` agree exactly.

    That agreement is why the column is authored: it is a property of the snapshot, so the
    day it stops holding, nothing in the code has to change.
    """
    disagreements = [
        n.code for n in nodes() if n.level == "code" and n.is_terminal != (len(n.code) == 10)
    ]
    assert disagreements == []


def test_code_length_distribution() -> None:
    lengths = Counter(len(n.code) for n in nodes() if n.level == "code")
    assert lengths == {6: 1_719, 8: 903, 10: 10_490}


def test_natural_key_is_unique() -> None:
    keys = Counter(n.key for n in nodes())
    assert [k for k, c in keys.items() if c > 1] == []


def test_section_group_collision_is_real_and_survivable() -> None:
    """All 21 section codes are also group codes — the bug the natural key deletes."""
    sections = {n.code for n in nodes() if n.level == "section"}
    groups = {n.code for n in nodes() if n.level == "group"}
    assert sections <= groups
    assert len(sections & groups) == 21
    # And the two are genuinely different rows with different subtrees.
    index = by_code()
    for code in sorted(sections):
        assert index[("section", code)].full_path != index[("group", code)].full_path


def test_prefix_integrity() -> None:
    """Every prefix-tree code extends its parent's code, with no synthesised parents."""
    index = by_code()
    for node in nodes():
        if node.level != "code":
            continue
        parent_level, parent_code = node.parent_key
        assert node.code.startswith(parent_code)
        assert len(parent_code) < len(node.code)
        assert (parent_level, parent_code) in index
        assert index[(parent_level, parent_code)].code == node.ancestor_codes[-1]


def test_missing_intermediate_parents_are_not_invented() -> None:
    """3,236 ten-digit codes legitimately hang straight off the 4-digit category."""
    orphans = [
        n for n in nodes() if n.level == "code" and len(n.code) == 10 and n.depth == 3
    ]
    assert len(orphans) == 3_236
    assert all(n.parent_key[0] == "category" for n in orphans)


def test_depth_distribution() -> None:
    depths = Counter(n.depth for n in nodes())
    assert max(depths) == EXPECTED.max_depth == 5
    assert depths == {0: 21, 1: 97, 2: 957, 3: 5_343, 4: 6_551, 5: 1_218}


def test_full_path_is_never_empty_and_never_bare() -> None:
    assert all(n.full_path.strip() for n in nodes())
    # Every node below a section carries its ancestors, so no payload is ever just "інші".
    assert all(" > " in n.full_path for n in nodes() if n.level != "section")


def test_bare_descriptions_would_be_useless() -> None:
    """The reason full_path is mandatory: 2,473 leaves are literally "інші"."""
    terminals = [n for n in nodes() if n.is_terminal]
    assert sum(1 for n in terminals if n.description.lower() == "інші") == 2_473
    assert sum(1 for n in terminals if len(n.description) <= 15) / len(terminals) > 0.45


def test_ambiguous_paths_are_flagged() -> None:
    ambiguous = [n for n in nodes() if n.path_is_ambiguous]
    assert len(ambiguous) == 2_487
    assert all(n.is_terminal for n in ambiguous)


def test_empty_group_77_is_a_dead_end_not_a_crash() -> None:
    """Section 15 / group 77 has no `children` key at all."""
    dead_ends = [n for n in nodes() if n.is_dead_end]
    assert len(dead_ends) == 1
    group = dead_ends[0]
    assert (group.level, group.code) == ("group", "77")
    assert group.description == "Група 77"
    assert group.ancestor_codes == ["15"]
    assert group.child_count == 0
    assert not group.is_terminal


def test_colon_cross_check() -> None:
    """A free check on `is_terminal`: internal nodes end with ':', leaves almost never do."""
    terminals = [n for n in nodes() if n.is_terminal]
    assert sum(1 for n in terminals if n.raw_description.rstrip().endswith(":")) == 6


def test_known_example_full_path() -> None:
    node = by_code()[("code", EXAMPLE_CODE)]
    assert node.full_path == " > ".join(EXAMPLE_PATH_PARTS)
    assert node.is_terminal
    assert node.depth == 4
    assert node.ancestor_codes == ["07", "39", "3919", "391910"]
    assert node.parent_key == ("code", "391910")
    assert node.sort_key == "07/39/3919/391910/3919101200"


def test_sort_key_is_document_order() -> None:
    """ORDER BY sort_key renders the whole tree depth-first, parents before children."""
    ordered = sorted(nodes(), key=lambda n: n.sort_key)
    assert ordered[0].code == "01"
    assert ordered[0].level == "section"
    seen: set[str] = set()
    for node in ordered:
        if node.parent_key is not None:
            assert "/".join([*node.ancestor_codes, node.code]) == node.sort_key
            parent_sort = node.sort_key.rsplit("/", 1)[0]
            assert parent_sort in seen
        seen.add(node.sort_key)


def test_invariants_fail_loudly() -> None:
    """The point of the assertions: a broken load raises, it does not warn.

    Written without `pytest.raises` so the whole module stays importable with nothing
    installed at all — that is what makes it runnable as a plain script.
    """
    broken = list(nodes())[:-1]
    try:
        check_invariants(broken)
    except IngestInvariantError as exc:
        assert any("node count" in failure for failure in exc.failures)
    else:
        raise AssertionError("check_invariants accepted a truncated node list")


def test_build_is_deterministic() -> None:
    _, sections = load_source(SOURCE)
    first = build_nodes(sections)
    second = build_nodes(sections)
    assert [n.sort_key for n in first] == [n.sort_key for n in second]


if __name__ == "__main__":  # pragma: no cover - the self-check path, no pytest needed
    all_nodes = nodes()
    levels = Counter(n.level for n in all_nodes)
    print(f"source                {SOURCE}")
    print(f"sha256                {load_source(SOURCE)[0]}")
    print(f"nodes                 {len(all_nodes):,}")
    print(f"  sections            {levels['section']:,}")
    print(f"  groups              {levels['group']:,}")
    print(f"  categories          {levels['category']:,}")
    print(f"  prefix-tree codes   {levels['code']:,}")
    print(f"terminals             {sum(1 for n in all_nodes if n.is_terminal):,}")
    print(f"ambiguous full_path   {sum(1 for n in all_nodes if n.path_is_ambiguous):,}")
    print(f"dead ends             {[n.code for n in all_nodes if n.is_dead_end]}")
    print(f"max depth             {max(n.depth for n in all_nodes)}")
    print(f"empty full_path       {sum(1 for n in all_nodes if not n.full_path.strip())}")
    failures = 0
    for name, case in sorted(globals().items()):
        if name.startswith("test_") and callable(case):
            try:
                case()
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
            else:
                print(f"ok   {name}")
    sys.exit(1 if failures else 0)
