"""The navigation SQL, executed by a real SQL engine against a miniature tariff.

`app/tariff/repo.py` is nothing but queries, so a test that mocks the queries away tests
nothing at all. This file reuses the technique from `tests/test_records_writer.py`: `FakePool`
is an asyncpg-shaped double over in-memory SQLite that RUNS the repo's real SQL (`$n` → `?`,
`::text` casts dropped) against a schema mirroring migration 0003 — natural key, CHECKs and
all. The recursive CTEs behind `ancestry` and `open_category` are therefore evaluated by an
actual engine, which is the only way an assertion about a breadcrumb's ORDER can mean
anything.

Two Postgres-isms are translated rather than mocked:

* `ancestor_codes` is `text[]`. Here it is JSON in a column declared `TEXT_ARRAY`, decoded by
  a `sqlite3` converter, so `NodeDetail.ancestor_codes` arrives as the list it does in
  production instead of a string that `list()` would silently shred into characters.
* `word_similarity()` is `pg_trgm`. `_word_similarity` below is a faithful-enough stand-in
  (padded per-word trigrams, best continuous word extent), registered as a SQL function. No
  assertion depends on an exact score — what is under test is the SQL around it: the
  dataset filter, the `NOT is_dead_end` exclusion, the score floor and the ORDER BY.

The fixture is a 5-section tariff that reproduces, in miniature, every anomaly the real
14,187-row dataset has and that this module exists to survive: a section code that is also a
group code (07), groups stored out of code order (section 02), a reserved empty group (15/77),
two sections sharing a description (19/20), leaves described literally «інші», a 10-digit code
whose 8-digit parent does not exist, and one category fat enough to truncate.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any

import pytest

from app.agent.schemas import TreeNode
from app.tariff import PATH_SEPARATOR
from app.tariff.catalogue import build_sections_catalogue, catalogue_digest
from app.tariff.repo import (
    MAX_TREE_CHARS,
    MIN_SEARCH_SCORE,
    NoActiveDatasetError,
    TariffRepo,
    UnknownCodeError,
    level_for_code,
    normalize_code,
)

# --------------------------------------------------------------------------------------
# migration 0003, in SQLite. Postgres types swapped, every constraint kept.
# --------------------------------------------------------------------------------------

_SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE tariff_dataset (
    id              INTEGER     PRIMARY KEY,
    source_filename TEXT        NOT NULL,
    sha256          TEXT        NOT NULL UNIQUE,
    node_count      INTEGER     NOT NULL,
    terminal_count  INTEGER     NOT NULL,
    is_active       INTEGER     NOT NULL DEFAULT 0,
    ingested_at     TIMESTAMPTZ NOT NULL
);

-- At most one active dataset, enforced by the database and not by the ingest code.
CREATE UNIQUE INDEX tariff_dataset_one_active
    ON tariff_dataset (is_active) WHERE is_active;

CREATE TABLE tariff_node (
    id                INTEGER    PRIMARY KEY,
    dataset_id        INTEGER    NOT NULL REFERENCES tariff_dataset(id) ON DELETE CASCADE,
    code              TEXT       NOT NULL,
    level             TEXT       NOT NULL,
    depth             INTEGER    NOT NULL,
    parent_id         INTEGER    REFERENCES tariff_node(id) ON DELETE CASCADE,
    description       TEXT       NOT NULL,
    full_path         TEXT       NOT NULL,
    ancestor_codes    TEXT_ARRAY NOT NULL,
    is_terminal       INTEGER    NOT NULL,
    child_count       INTEGER    NOT NULL,
    path_is_ambiguous INTEGER    NOT NULL DEFAULT 0,
    is_dead_end       INTEGER    NOT NULL DEFAULT 0,
    sort_key          TEXT       NOT NULL,
    CONSTRAINT tariff_node_natural_key UNIQUE (dataset_id, level, code),
    CONSTRAINT tariff_node_level CHECK (level IN ('section', 'group', 'category', 'code')),
    CONSTRAINT tariff_node_full_path_not_empty CHECK (length(full_path) > 0),
    CONSTRAINT tariff_node_terminal_is_leaf CHECK (NOT is_terminal OR child_count = 0),
    CONSTRAINT tariff_node_dead_end
        CHECK (is_dead_end = (NOT is_terminal AND child_count = 0))
);
"""

DATASET_ID = 1
DATASET_SHA = "e" * 64

# `ancestor_codes` is text[] in Postgres; asyncpg hands back a list, so the double must too.
sqlite3.register_converter("TEXT_ARRAY", lambda raw: json.loads(raw.decode("utf-8")))
sqlite3.register_converter("TIMESTAMPTZ", lambda raw: datetime.fromisoformat(raw.decode()))


# --------------------------------------------------------------------------------------
# The miniature tariff
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Spec:
    """One node of the source file, before it becomes rows."""

    code: str
    description: str
    children: tuple[Spec, ...] = ()
    reserved: bool = False


def _n(code: str, description: str, *children: Spec, reserved: bool = False) -> Spec:
    return Spec(code, description, children, reserved)


def _fat_category() -> Spec:
    """A category whose subtree serialises past `MAX_TREE_CHARS` — the truncating branch.

    Measured payloads run to 18,820 characters, so this is the shape of a real heading and
    not a synthetic stress case: 15 six-digit headings of three terminals each.
    """
    thickness = (
        "0,1", "0,2", "0,5", "1", "2", "3", "5", "8",
        "10", "12", "15", "20", "25", "30", "50",
    )  # fmt: skip
    children = []
    for index, limit in enumerate(thickness, start=10):
        heading = f"3920{index:02d}"
        leaves = [
            _n(
                f"{heading}{leaf:02d}00",
                f"завтовшки не більш як {limit} мм, неармовані, без підкладки, "
                f"не сполучені подібним способом з іншими матеріалами, варіант {leaf}",
            )
            for leaf in (1, 2, 9)
        ]
        children.append(
            _n(
                heading,
                f"з полімерів вінілхлориду, непористі, завтовшки не більш як {limit} мм",
                *leaves,
            )
        )
    return _n("3920", "Інші плити, листи, плівки, стрічки та смуги, з пластмас", *children)


def _one_group(code: str, description: str, category: str, title: str) -> Spec:
    """A group with a single terminal category — a group whose contents are beside the point.

    A group with no children at all would be `is_dead_end`, which the schema CHECK enforces
    and which this fixture reserves for the one group that really is (15/77).
    """
    return _n(code, description, _n(category, title))


# Sections are listed OUT of code order so that ORDER BY sort_key is doing real work: a
# builder that returned insertion order would pass every ordering assertion by accident.
TARIFF: tuple[Spec, ...] = (
    _n(
        "07",
        "Пластмаси, каучук та вироби з них",
        _n(
            "39",
            "Пластмаси, полімерні матеріали та вироби з них",
            _n(
                "3919",
                "Плити, листи, стрічки, смуги, плівки та інші плоскі форми, самоклейні, "
                "пластмасові",
                _n(
                    "391910",
                    "у рулонах завширшки не більш як 20 см",
                    _n("39191012", "з полівінілхлориду", _n("3919101200", "інші")),
                    # No 8-digit parent exists for this one, so ingest hangs it off the
                    # longest prefix that does — 3,236 real codes are like this.
                    _n("3919101100", "з поліетилену"),
                ),
                _n(
                    "391990",
                    "інші",
                    # Two siblings, both «інші», therefore one shared full_path: the
                    # 23.71% of terminals this dataset genuinely cannot separate.
                    _n("3919900010", "інші"),
                    _n("3919900090", "інші"),
                ),
            ),
            _fat_category(),
        ),
        # A chain deep enough to outrun MAX_SUBTREE_DEPTH when opened from the group.
        _n(
            "40",
            "Каучук, гума та вироби з них",
            _n(
                "4001",
                "Каучук натуральний, балата, гутаперча, гваюла, чикл",
                _n(
                    "400110",
                    "латекс каучуку натурального",
                    _n("40011010", "попередньо вулканізований", _n("4001101000", "інші")),
                ),
            ),
        ),
    ),
    _n(
        "02",
        "Продукти рослинного походження",
        # Stored 09, 10, 07, 08 — the shape that makes a naive builder print "(групи 09–08)".
        _one_group("09", "Кава, чай, мате (парагвайський чай) і прянощі", "0901", "Кава"),
        _one_group("10", "Зернові культури", "1001", "Пшениця і суміш пшениці та жита"),
        # Group 07 and section 07 are different rows with the same two digits.
        _one_group("07", "Овочі та деякі їстівні коренеплоди і бульби", "0701", "Картопля"),
        _one_group("08", "Їстівні плоди та горіхи", "0801", "Горіхи кокосові"),
    ),
    _n(
        "19",
        "Різні промислові товари",
        _one_group("93", "Зброя та боєприпаси", "9301", "Зброя військова"),
        _one_group("94", "Меблі", "9401", "Меблі для сидіння"),
        _one_group("95", "Іграшки, ігри та спортивний інвентар", "9503", "Іграшки"),
        _one_group("96", "Різні готові вироби", "9601", "Кістка оброблена"),
    ),
    _n(
        "15",
        "Недорогоцінні метали та вироби з них",
        _one_group("72", "Чорні метали", "7208", "Прокат плоский з вуглецевої сталі"),
        # Reserved and empty: no children key at all in the source file.
        _n("77", "Група 77", reserved=True),
    ),
    _n(
        "20",
        "Різні промислові товари",
        _one_group("97", "Твори мистецтва", "9701", "Картини та малюнки"),
    ),
)

_LEVEL_BY_DEPTH = ("section", "group", "category")


def _build_rows(specs: Sequence[Spec]) -> list[dict[str, Any]]:
    """The source tree → `tariff_node` rows, the way `app/tariff/ingest.py` builds them."""
    rows: list[dict[str, Any]] = []

    def walk(spec: Spec, ancestors: list[str], prefix: str, parent_id: int | None) -> None:
        assert not (spec.reserved and spec.children), "a reserved group has no children"
        depth = len(ancestors)
        full_path = f"{prefix}{PATH_SEPARATOR}{spec.description}" if prefix else spec.description
        row_id = len(rows) + 1
        rows.append(
            {
                "id": row_id,
                "dataset_id": DATASET_ID,
                "code": spec.code,
                "level": _LEVEL_BY_DEPTH[depth] if depth < 3 else "code",
                "depth": depth,
                "parent_id": parent_id,
                "description": spec.description,
                "full_path": full_path,
                "ancestor_codes": json.dumps(ancestors),
                "is_terminal": int(not spec.children and not spec.reserved),
                "child_count": len(spec.children),
                "path_is_ambiguous": 0,
                "is_dead_end": int(spec.reserved),
                "sort_key": "/".join([*ancestors, spec.code]),
            }
        )
        for child in spec.children:
            # The prefix tree is a prefix tree: ingest derives parentage from the string.
            assert depth < 2 or child.code.startswith(spec.code), child.code
            walk(child, [*ancestors, spec.code], full_path, row_id)

    for spec in specs:
        walk(spec, [], "", None)

    shared: dict[str, int] = {}
    for row in rows:
        if row["is_terminal"]:
            shared[row["full_path"]] = shared.get(row["full_path"], 0) + 1
    for row in rows:
        row["path_is_ambiguous"] = int(row["is_terminal"] and shared[row["full_path"]] > 1)
    return rows


ROWS = _build_rows(TARIFF)


# --------------------------------------------------------------------------------------
# pg_trgm, approximately
# --------------------------------------------------------------------------------------


@lru_cache(maxsize=4096)
def _trigrams(text: str) -> frozenset[str]:
    """pg_trgm's tokenisation: each word padded with two leading spaces and one trailing."""
    grams: set[str] = set()
    for word in re.findall(r"\w+", text.lower()):
        padded = f"  {word} "
        grams.update(padded[i : i + 3] for i in range(len(padded) - 2))
    return frozenset(grams)


def _word_similarity(query: str, target: str) -> float:
    """The best similarity between `query`'s trigrams and any continuous word extent of
    `target` — `word_similarity()`, near enough that the SQL around it can be tested."""
    needle = _trigrams(query)
    if not needle:
        return 0.0
    words = re.findall(r"\w+", target.lower())
    span = len(re.findall(r"\w+", query.lower())) + 2
    best = 0.0
    for start in range(len(words)):
        for end in range(start + 1, min(start + span, len(words)) + 1):
            extent = _trigrams(" ".join(words[start:end]))
            best = max(best, len(needle & extent) / len(needle | extent))
    return best


# --------------------------------------------------------------------------------------
# The asyncpg surface, over SQLite
# --------------------------------------------------------------------------------------

_PARAM = re.compile(r"\$(\d+)")
_CAST = re.compile(r"::\w+")


def _translate(sql: str, args: tuple[Any, ...]) -> tuple[str, list[Any]]:
    """asyncpg dialect → sqlite3 dialect: numbered parameters and casts."""
    collected: list[Any] = []

    def _sub(match: re.Match[str]) -> str:
        collected.append(args[int(match.group(1)) - 1])
        return "?"

    return _CAST.sub("", _PARAM.sub(_sub, sql)), collected


class FakePool:
    """The three statement methods `TariffRepo`'s `Executor` protocol asks for."""

    def __init__(self, *, active: bool = True) -> None:
        self.conn = sqlite3.connect(":memory:", detect_types=sqlite3.PARSE_DECLTYPES)
        self.conn.row_factory = sqlite3.Row
        self.conn.create_function("word_similarity", 2, _word_similarity, deterministic=True)
        self.conn.executescript(_SCHEMA)
        self.queries: list[str] = []
        self.conn.execute(
            "INSERT INTO tariff_dataset (id, source_filename, sha256, node_count,"
            " terminal_count, is_active, ingested_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                DATASET_ID,
                "uktzed_hierarchical.json",
                DATASET_SHA,
                len(ROWS),
                sum(r["is_terminal"] for r in ROWS),
                int(active),
                datetime(2026, 9, 16, 12, 0, tzinfo=UTC).isoformat(),
            ),
        )
        columns = ", ".join(ROWS[0])
        placeholders = ", ".join("?" * len(ROWS[0]))
        self.conn.executemany(
            f"INSERT INTO tariff_node ({columns}) VALUES ({placeholders})",
            [tuple(row.values()) for row in ROWS],
        )
        self.conn.commit()

    def _run(self, sql: str, args: tuple[Any, ...]) -> list[sqlite3.Row]:
        self.queries.append(sql)
        translated, params = _translate(sql, args)
        return self.conn.execute(translated, params).fetchall()

    async def fetch(self, query: str, *args: Any) -> list[Any]:
        return list(self._run(query, args))

    async def fetchrow(self, query: str, *args: Any) -> Any:
        rows = self._run(query, args)
        return rows[0] if rows else None

    async def fetchval(self, query: str, *args: Any) -> Any:
        rows = self._run(query, args)
        return rows[0][0] if rows else None


@pytest.fixture
def pool() -> Iterator[FakePool]:
    fake = FakePool()
    yield fake
    fake.conn.close()


@pytest.fixture
def repo(pool: FakePool) -> TariffRepo:
    return TariffRepo(pool)


@pytest.fixture
def uningested() -> Iterator[TariffRepo]:
    """The same tariff, with no dataset marked active — a database before the ingest ran."""
    fake = FakePool(active=False)
    yield TariffRepo(fake)
    fake.conn.close()


def _codes(nodes: Sequence[Any]) -> list[str]:
    return [node.code for node in nodes]


def _flatten(nodes: Sequence[TreeNode]) -> list[TreeNode]:
    out: list[TreeNode] = []
    for node in nodes:
        out.append(node)
        out.extend(_flatten(node.children))
    return out


# --------------------------------------------------------------------------------------
# The active dataset
# --------------------------------------------------------------------------------------


async def test_the_active_dataset_is_read_once_and_reused(repo: TariffRepo, pool: FakePool) -> None:
    """Every navigation query is parameterised by the dataset id, so resolving it per call
    would double the round trips on the hot path for a value that cannot change."""
    dataset = await repo.active_dataset()

    assert dataset.id == DATASET_ID
    assert dataset.sha256 == DATASET_SHA  # /readyz compares this against the file on disk
    assert dataset.node_count == len(ROWS)
    assert isinstance(dataset.ingested_at, datetime)

    await repo.list_sections()
    await repo.list_sections()
    assert sum("tariff_dataset" in q for q in pool.queries) == 1


async def test_an_empty_database_says_so_instead_of_answering_nothing(
    uningested: TariffRepo,
) -> None:
    """A tariff that was never ingested must not look like a tariff with no sections —
    `/readyz` turns this into a 503, and v1's silence here cost two months."""
    with pytest.raises(NoActiveDatasetError):
        await uningested.list_sections()


# --------------------------------------------------------------------------------------
# The (level, code) natural key: a section is not a group
# --------------------------------------------------------------------------------------


async def test_a_two_digit_code_is_always_a_group_never_a_section(repo: TariffRepo) -> None:
    """THE bug this schema deletes. Section 07 is «Пластмаси…»; group 07 is «Овочі…». v1
    accepted a bare "07" for both and silently walked into the wrong subtree."""
    assert level_for_code("07") == "group"

    node = await repo.resolve("07")

    assert node is not None
    assert node.level == "group"
    assert node.description == "Овочі та деякі їстівні коренеплоди і бульби"
    assert node.ancestor_codes == ["02"], "group 07 lives under section 02, not section 07"


async def test_the_section_and_the_group_that_share_two_digits_are_different_rows(
    repo: TariffRepo,
) -> None:
    """Both rows exist, both are reachable, and neither one is reachable by the other's
    route: `list_groups_in_section` is the only entry point that takes a section code."""
    groups_of_section_07 = await repo.list_groups_in_section("07")
    categories_of_group_07 = await repo.list_categories_in_group("07")

    assert _codes(groups_of_section_07) == ["39", "40"]
    assert _codes(categories_of_group_07) == ["0701"]
    # …and the group that IS section 07's namesake hangs off a different section entirely.
    assert "07" in _codes(await repo.list_groups_in_section("02"))


async def test_the_ancestry_of_a_two_digit_code_is_the_groups_ancestry(repo: TariffRepo) -> None:
    """The recursive walk goes up by parent_id, so it inherits the natural key rather than
    re-deriving a level from two digits that belong to two different namespaces."""
    assert _codes(await repo.ancestry("07")) == ["02"]


# --------------------------------------------------------------------------------------
# list_sections / list_groups_in_section / list_categories_in_group
# --------------------------------------------------------------------------------------


async def test_list_sections_returns_every_section_in_code_order(repo: TariffRepo) -> None:
    sections = await repo.list_sections()

    assert _codes(sections) == ["02", "07", "15", "19", "20"]
    assert sections[0].description == "Продукти рослинного походження"


async def test_the_group_range_comes_from_min_max_not_from_arrival_order(
    repo: TariffRepo,
) -> None:
    """Section 02's groups are stored 09, 10, 07, 08 — exactly the shape that makes a builder
    reading first/last child emit the nonsense range «групи 09–08»."""
    section = next(s for s in await repo.list_sections() if s.code == "02")

    assert (section.first_group_code, section.last_group_code) == ("07", "10")
    assert section.group_count == 4


async def test_groups_of_a_section_come_back_in_code_order(repo: TariffRepo) -> None:
    assert _codes(await repo.list_groups_in_section("02")) == ["07", "08", "09", "10"]


async def test_a_reserved_group_is_listed_but_opens_empty(repo: TariffRepo) -> None:
    """Group 77 is in the tariff, so the drill-down shows it. What it is not is a mistake:
    an empty list on its own invites the model to retry, so callers read `is_dead_end`."""
    assert "77" in _codes(await repo.list_groups_in_section("15"))
    assert await repo.list_categories_in_group("77") == []

    reserved = await repo.resolve("77")
    assert reserved is not None
    assert reserved.is_dead_end is True
    assert reserved.child_count == 0


async def test_an_unknown_group_and_a_reserved_one_look_the_same_until_resolved(
    repo: TariffRepo,
) -> None:
    """Both answer `[]`; only `resolve()` tells them apart. Pinned so that nobody 'fixes'
    the empty list into an exception and takes the reserved group with it."""
    assert await repo.list_categories_in_group("99") == []
    assert await repo.resolve("99") is None


async def test_a_code_is_normalised_at_the_boundary(repo: TariffRepo) -> None:
    """`XXXX XX XX XX` is what a human pastes and what the model echoes. v1 delegated the
    format rule to model obedience in three places."""
    assert normalize_code(" 3919 10 12-00\u00a0") == "3919101200"

    spaced = await repo.resolve("3919 10 12 00")
    assert spaced is not None
    assert spaced.code == "3919101200"
    assert _codes(await repo.list_groups_in_section(" 1.5 ")) == ["72", "77"]


# --------------------------------------------------------------------------------------
# resolve
# --------------------------------------------------------------------------------------


async def test_resolve_reads_full_path_and_is_terminal_from_the_database(
    repo: TariffRepo,
) -> None:
    """The two fields nothing downstream may reconstruct: `full_path`, because the leaf is
    called «інші», and `is_terminal`, because in v1 `is_final` was a prompt promise."""
    node = await repo.resolve("3919101200")

    assert node is not None
    assert node.description == "інші"
    assert node.full_path == (
        "Пластмаси, каучук та вироби з них"
        " > Пластмаси, полімерні матеріали та вироби з них"
        " > Плити, листи, стрічки, смуги, плівки та інші плоскі форми, самоклейні, пластмасові"
        " > у рулонах завширшки не більш як 20 см"
        " > з полівінілхлориду"
        " > інші"
    )
    assert node.is_terminal is True
    assert node.level == "code"
    assert node.depth == 5
    assert node.ancestor_codes == ["07", "39", "3919", "391910", "39191012"]
    assert node.section_code == "07"
    assert node.child_count == 0


async def test_resolve_reports_a_full_path_shared_with_a_sibling(repo: TariffRepo) -> None:
    """2,487 terminals (23.71%) are indistinguishable by text alone. The answer has to show
    the whole set, so the flag is carried rather than quietly deduplicated."""
    ambiguous = await repo.resolve("3919900010")
    sibling = await repo.resolve("3919900090")
    unique = await repo.resolve("3919101100")

    assert ambiguous is not None and sibling is not None and unique is not None
    assert ambiguous.path_is_ambiguous is True
    assert sibling.path_is_ambiguous is True
    assert unique.path_is_ambiguous is False
    assert ambiguous.full_path == sibling.full_path
    assert ambiguous.code != sibling.code


async def test_resolve_returns_none_for_a_code_that_is_not_in_the_dataset(
    repo: TariffRepo,
) -> None:
    """`None` is the answer the validation gate turns into E2 — v1 never checked, and a
    hallucinated code reached the user as an answer."""
    assert await repo.resolve("9999999999") is None
    assert await repo.resolve("0000") is None


async def test_resolve_refuses_an_empty_code_without_touching_the_database(
    repo: TariffRepo, pool: FakePool
) -> None:
    """A code of punctuation normalises to "", which as a query would be an unindexed scan
    returning nothing — and `ancestry` would answer the same way for a different reason."""
    assert await repo.resolve("—") is None
    assert await repo.ancestry("  ") == []
    assert not any("tariff_node" in q for q in pool.queries)


# --------------------------------------------------------------------------------------
# ancestry
# --------------------------------------------------------------------------------------


async def test_ancestry_returns_the_chain_root_first(repo: TariffRepo) -> None:
    """The breadcrumb, and the ORDER is the contract: `ORDER BY depth` on the recursive walk
    is what makes «Розділ 07 › 39 › 3919 › …» readable rather than reversed."""
    chain = await repo.ancestry("3919101200")

    assert _codes(chain) == ["07", "39", "3919", "391910", "39191012"]
    assert [step.level for step in chain] == ["section", "group", "category", "code", "code"]
    assert chain[0].description == "Пластмаси, каучук та вироби з них"
    assert chain[-1].description == "з полівінілхлориду"
    # Excludes the code itself — the caller already has it.
    assert "3919101200" not in _codes(chain)


async def test_ancestry_of_a_missing_code_is_empty(repo: TariffRepo) -> None:
    """Empty, not an exception: a caller that needs to tell "no ancestors" from "no such
    code" has already called `resolve`."""
    assert await repo.ancestry("9999999999") == []


# --------------------------------------------------------------------------------------
# open_category / expand
# --------------------------------------------------------------------------------------


async def test_open_category_builds_the_nested_prefix_tree(repo: TariffRepo) -> None:
    """6 → 8 → 10 digits nest by string prefix, because that is how the source stores them:
    one flat list per category, with parentage recoverable only from the digits.

    A flat payload here would hand the model 14 sibling codes with no structure and no way
    to see that 3919101200 sits under «з полівінілхлориду» rather than beside it.
    """
    tree = await repo.open_category("3919")

    assert tree.truncated is False
    assert _codes(tree.nodes) == ["391910", "391990"]

    six, ninety = tree.nodes
    # Document order is `sort_key`, i.e. the codes as strings: …1100 precedes …12.
    assert _codes(six.children) == ["3919101100", "39191012"]
    assert _codes(six.children[1].children) == ["3919101200"]
    assert _codes(ninety.children) == ["3919900010", "3919900090"]

    # The structural invariant, checked rather than assumed: every child extends its parent.
    def assert_nests(nodes: Sequence[TreeNode], prefix: str) -> None:
        for node in nodes:
            assert node.code.startswith(prefix), f"{node.code} is not under {prefix}"
            assert_nests(node.children, node.code)

    assert_nests(tree.nodes, "3919")
    assert tree.terminal_count == 4
    assert [n.is_terminal for n in _flatten(tree.nodes)].count(True) == 4
    assert not any(n.collapsed for n in _flatten(tree.nodes))


async def test_open_category_keeps_a_ten_digit_code_whose_eight_digit_parent_is_missing(
    repo: TariffRepo,
) -> None:
    """3,236 real ten-digit codes have no six- or eight-digit parent in the source. Ingest
    hangs them off the longest EXISTING prefix instead of synthesising a row whose text is
    not in the tariff, and the tree walk has to show them at that level."""
    tree = await repo.open_category("3919")
    orphan = next(n for n in _flatten(tree.nodes) if n.code == "3919101100")

    assert orphan.is_terminal is True
    # It sits beside the 8-digit heading, not under it: nothing invents a 39191011 row.
    assert _codes(tree.nodes[0].children) == ["3919101100", "39191012"]


async def test_open_category_carries_the_breadcrumb_ending_at_itself(repo: TariffRepo) -> None:
    """`path` ends with the node, so the model prints the breadcrumb verbatim instead of
    reassembling one from codes it would have to guess the levels of."""
    tree = await repo.open_category("3919")

    assert [step.code for step in tree.path] == ["07", "39", "3919"]
    assert [step.level for step in tree.path] == ["section", "group", "category"]
    assert tree.path[-1].description == tree.description
    assert tree.full_path.endswith(tree.description)
    assert tree.is_dead_end is False


async def test_open_category_rejects_a_code_that_is_not_in_the_dataset(
    repo: TariffRepo,
) -> None:
    """The caller owns the Ukrainian message, so the exception carries what the message
    needs — the code and the level that was looked up — and nothing else."""
    with pytest.raises(UnknownCodeError) as raised:
        await repo.open_category("9999")

    assert raised.value.code == "9999"
    assert raised.value.level == "category"


async def test_a_subtree_deeper_than_the_depth_cut_is_marked_collapsed(
    repo: TariffRepo,
) -> None:
    """Group 40 is four levels above its deepest code, so `MAX_SUBTREE_DEPTH` cuts the last
    one. A node whose children are not in the payload says so — otherwise the model reads a
    cut branch as a terminal one and classifies into a heading."""
    tree = await repo.expand("40")

    assert tree.level == "group"
    assert tree.truncated is False, "this one is cut by DEPTH, not by size"
    assert _codes(tree.nodes) == ["4001"]
    cut = next(n for n in _flatten(tree.nodes) if n.code == "40011010")

    assert cut.children == []
    assert cut.is_terminal is False
    assert cut.collapsed is True, "children exist but were not fetched; say so"
    assert next(n for n in _flatten(tree.nodes) if n.code == "400110").collapsed is False


async def test_expand_reopens_the_tree_at_an_intermediate_code(repo: TariffRepo) -> None:
    """The escape hatch `open_category`'s truncated branch tells the model to use. The level
    is derived from the length, so a 6-digit prefix is a `code` and resolves as one."""
    tree = await repo.expand("391910")

    assert tree.level == "code"
    assert tree.code == "391910"
    assert _codes(tree.nodes) == ["3919101100", "39191012"]
    assert [step.code for step in tree.path] == ["07", "39", "3919", "391910"]


async def test_an_oversized_category_is_collapsed_to_one_level_and_says_so(
    repo: TariffRepo,
) -> None:
    """Adaptive, not fixed: ~90% of headings fit whole. The ones that do not come back as a
    single level plus `truncated`, which is the model's cue to call `expand` — the
    alternative is a payload that blows the context window on one tool call."""
    tree = await repo.open_category("3920")

    assert tree.truncated is True
    assert len(tree.nodes) == 15
    assert all(node.children == [] for node in tree.nodes)
    assert all(node.collapsed for node in tree.nodes)
    # The count is of the WHOLE subtree, including everything the truncation hid.
    assert tree.terminal_count == 45
    assert len(tree.model_dump_json(by_alias=True)) < MAX_TREE_CHARS


async def test_a_tree_that_fits_the_budget_is_returned_whole(repo: TariffRepo) -> None:
    """The other side of the adaptive cut-off: ~90% of headings never need `expand`, and
    paying a round trip for a payload of 1 KB would be the cure being worse."""
    tree = await repo.open_category("3919")

    assert len(tree.model_dump_json(by_alias=True)) < MAX_TREE_CHARS
    assert tree.truncated is False
    assert any(node.children for node in tree.nodes)


# --------------------------------------------------------------------------------------
# search_candidates
# --------------------------------------------------------------------------------------


async def test_search_matches_the_full_path_not_the_bare_description(
    repo: TariffRepo,
) -> None:
    """2,473 of 10,490 leaf descriptions are literally «інші». Matching on `description`
    would make a quarter of the terminal nomenclature unreachable and the rest ambiguous, so
    the trigram score is taken over the materialised `full_path`.

    The query below appears ONLY in an ancestor's text. A description-matcher scores every
    one of these leaves at zero.
    """
    hits = await repo.search_candidates("самоклейні пластмасові")

    assert hits, "the phrase is in the category heading, which is in every leaf's full_path"
    assert "3919101200" in _codes(hits)
    leaf = next(h for h in hits if h.code == "3919101200")
    assert leaf.description == "інші"
    assert "самоклейні, пластмасові" in leaf.full_path


async def test_search_never_returns_the_reserved_group(repo: TariffRepo) -> None:
    """`NOT is_dead_end` in the WHERE clause, and it is load-bearing: group 77 scores a
    perfect match on its own title and has nothing behind it to classify into."""
    assert _word_similarity("Група 77", "Недорогоцінні метали та вироби з них > Група 77") >= (
        MIN_SEARCH_SCORE
    ), "the fixture must make this row a genuine hit, or the exclusion proves nothing"

    assert _codes(await repo.search_candidates("Група 77")) == []


async def test_search_returns_nothing_for_an_empty_query(repo: TariffRepo, pool: FakePool) -> None:
    """Whitespace is not a hypothesis. Below the floor every row scores, so an unguarded
    empty query is a 14,187-row scan that returns the shortest paths in the tariff."""
    assert await repo.search_candidates("   \n ") == []
    assert not any("word_similarity" in q for q in pool.queries)


async def test_search_honours_the_limit(repo: TariffRepo) -> None:
    """A short list of plausible branches beats a long list of near-zero scores — and a
    limit of zero is a caller bug, not an instruction to return the whole tariff."""
    assert len(await repo.search_candidates("плівки самоклейні", limit=2)) <= 2
    assert len(await repo.search_candidates("плівки самоклейні", limit=0)) == 1


async def test_search_answers_with_the_fields_the_model_needs_to_open_a_branch(
    repo: TariffRepo,
) -> None:
    """Hypotheses, not an answer: every candidate carries the code and level that `expand`
    and `open_category` take, so the model can walk into one instead of emitting it."""
    hits = await repo.search_candidates("Чорні метали")

    assert hits
    assert all(hit.level in ("section", "group", "category", "code") for hit in hits)
    assert all(hit.full_path for hit in hits)
    assert "72" in _codes(hits)


# --------------------------------------------------------------------------------------
# The prompt catalogue
# --------------------------------------------------------------------------------------


async def test_the_catalogue_lists_every_section_with_both_numbering_systems(
    repo: TariffRepo,
) -> None:
    """Rule 2: `Розділ NN (групи A–B):`. v1 spent 622 characters of prose telling the model
    which 2-digit namespace a number belonged to; labelling both deletes the prose."""
    catalogue = await build_sections_catalogue(repo)
    headings = [line for line in catalogue.splitlines() if line.startswith("Розділ")]

    assert len(headings) == 5
    assert headings[0] == "Розділ 02 (групи 07–10): Продукти рослинного походження"
    assert headings == sorted(headings), "sections are listed in code order"
    assert "Розділ 15 (групи 72–77): Недорогоцінні метали та вироби з них" in headings


async def test_the_catalogue_skips_the_placeholder_group_title(repo: TariffRepo) -> None:
    """Rule 4. «Група 77» is a reserved, empty group whose title is its own number — a line
    that teaches the model nothing and invites it to classify into a hole."""
    catalogue = await build_sections_catalogue(repo)

    assert "Група 77" not in catalogue
    assert "    72 — Чорні метали" in catalogue, "its non-placeholder sibling is still listed"


async def test_the_catalogue_expands_sections_that_share_a_description(
    repo: TariffRepo,
) -> None:
    """Rule 3, the half that only the duplicate check can do. Sections 19 and 20 are both
    «Різні промислові товари» — with no group titles they are indistinguishable, and section
    19 has four groups, so the "at most three" rule would leave it bare."""
    catalogue = await build_sections_catalogue(repo)

    assert "    93 — Зброя та боєприпаси" in catalogue
    assert "    97 — Твори мистецтва" in catalogue


async def test_the_catalogue_leaves_an_unambiguous_section_unexpanded(
    repo: TariffRepo,
) -> None:
    """The control for the rule above: section 02 has a unique description and four groups,
    so its heading carries the range and the prompt stays short."""
    catalogue = await build_sections_catalogue(repo)

    assert "Кава, чай, мате" not in catalogue
    assert "Зернові культури" not in catalogue


async def test_the_catalogue_digest_changes_with_the_catalogue(repo: TariffRepo) -> None:
    """The digest is pinned into the prompt version and into every classification row, so
    "which catalogue produced this answer" survives the next re-ingest."""
    catalogue = await build_sections_catalogue(repo)
    digest = catalogue_digest(catalogue)

    assert len(digest) == 64
    assert digest == catalogue_digest(catalogue)
    assert digest != catalogue_digest(catalogue + "\n")


# --------------------------------------------------------------------------------------
# The dataclass the fixture is built on is the schema's, not a second one
# --------------------------------------------------------------------------------------


def test_the_fixture_mirrors_the_datasets_real_anomalies() -> None:
    """Guards the guards: if the miniature tariff loses one of these, the tests above start
    passing for the wrong reason."""
    by_code = {(r["level"], r["code"]): r for r in ROWS}

    assert ("section", "07") in by_code and ("group", "07") in by_code
    assert by_code[("group", "77")]["is_dead_end"] == 1
    assert by_code[("code", "3919900010")]["path_is_ambiguous"] == 1
    assert sum(1 for r in ROWS if r["description"] == "інші") >= 3
    assert by_code[("code", "3919101100")]["depth"] == 4
