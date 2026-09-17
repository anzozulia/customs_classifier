# the en dash and › are correct typography here, not Latin look-alikes; pyproject pins ruff's
# defaults for the rest of the alphabet.
"""The navigation tools: what they put in front of the model, and what they write down.

These six tools are the only way the tariff reaches the model, and they are the INPUT SIDE OF
THE GATE: `emit_classification` accepts a code only if a tool recorded it in THIS turn
(`tools_terminal`, E4). So a code a tool shows but never records is an answer the model cannot
give, and a code it records but never showed is an answer it can give without looking. Both are
gate failures, and both are what this file pins — together with the rule the whole data layer
exists for: **a bare 2-digit code is a GROUP**, everywhere except the one parameter that says
`section_code`.

The tools are driven through `FunctionTool.__wrapped__`, exactly as `tests/test_gate.py` does:
that skips the SDK's JSON-schema validation, which is the SDK's own well-tested machinery and
not the subject here.

`_Repo` is a fake `TariffRepo` over a real slice of the dataset — section 16 / group 85 /
heading 8517, the section-16-vs-group-16 collision, and the reserved group 15/77. It keys nodes
by `(level, code)`, the natural key Postgres uses, and derives that level with the repo's own
`level_for_code`, because a fake keyed by code alone would answer the collision tests by
construction instead of by behaviour. The adaptive cut-off is not faked either: `MAX_TREE_CHARS`
and the collapse are imported from `app.tariff.repo`, so a payload truncates here exactly where
it truncates in production.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import pytest
from agents import RunContextWrapper
from chatkit.types import ActiveStatus, ThreadMetadata

from app.agent import PathStep
from app.agent.context import UktzedContext
from app.agent.schemas import CategoryTree, NodeDetail, NodeSummary, TreeNode
from app.agent.tools_nav import (
    _EXPLAIN_AMBIGUOUS,
    _EXPLAIN_CANDIDATES,
    _EXPLAIN_CATEGORIES,
    _EXPLAIN_DEAD_END,
    _EXPLAIN_EMPTY_GROUP,
    _EXPLAIN_GROUPS,
    _EXPLAIN_NO_CANDIDATES,
    _EXPLAIN_RETRY,
    _EXPLAIN_TERMINAL,
    _EXPLAIN_TRUNCATED,
    MAX_SEARCH_LIMIT,
    expand,
    list_categories_in_group,
    list_groups_in_section,
    open_category,
    resolve_code,
    search_candidates,
)
from app.agent.tools_terminal import ClassifiedCode, emit_classification
from app.context import RequestContext
from app.tariff import PATH_SEPARATOR, TariffLevel
from app.tariff.repo import MAX_TREE_CHARS, UnknownCodeError, _collapse, level_for_code

# --------------------------------------------------------------------------------------
# A slice of the real dataset. Descriptions are the stored ones, shortened where the tariff
# runs to three lines; the shapes — which group sits in which section, which heading has an
# intermediate level under it — are not invented.
# --------------------------------------------------------------------------------------

SECTION_04 = "Готові харчові продукти; алкогольні та безалкогольні напої і оцет"
SECTION_15 = "Недорогоцінні метали та вироби з них"
SECTION_16 = "Машини, обладнання та механізми; електротехнічне обладнання"

GROUP_16 = "Готові харчові продукти з м’яса, риби або ракоподібних"
GROUP_76 = "Алюміній та вироби з нього"
GROUP_77 = "Група 77"
GROUP_84 = "Реактори ядерні, котли, машини, обладнання і механічні пристрої"
GROUP_85 = "Електричні машини, обладнання та їх частини"

CAT_1602 = "Інші готові чи консервовані продукти з м’яса, м’ясних субпродуктів або крові"
CAT_1604 = "Готова або консервована риба; ікра осетрових та ікра інших риб"
CAT_7601 = "Алюміній необроблений"
CAT_8501 = "Двигуни та генератори, електричні"
CAT_8517 = "Телефонні апарати, включаючи смартфони"

SMARTPHONES = "смартфони"
CORDED = "телефонні апарати для дротового зв’язку з бездротовою трубкою"
OTHER = "інша:"
VIDEOPHONES = "відеотелефони"
INTERCOMS = "переговорні пристрої"


def _path(*parts: str) -> str:
    """A stored `full_path`: the same concatenation ingest performed."""
    return PATH_SEPARATOR.join(parts)


def _summary(
    code: str, level: TariffLevel, description: str, full_path: str, *, terminal: bool = False
) -> NodeSummary:
    return NodeSummary(
        code=code,
        level=level,
        description=description,
        full_path=full_path,
        is_terminal=terminal,
    )


def _step(level: TariffLevel, code: str, description: str) -> PathStep:
    return PathStep(level=level, code=code, description=description)


def _leaf(code: str, description: str) -> TreeNode:
    return TreeNode(
        code=code, description=description, is_terminal=True, collapsed=False, children=[]
    )


SEC_04_ROW = _summary("04", "section", SECTION_04, SECTION_04)
SEC_15_ROW = _summary("15", "section", SECTION_15, SECTION_15)
SEC_16_ROW = _summary("16", "section", SECTION_16, SECTION_16)

GROUPS_BY_SECTION: dict[str, list[NodeSummary]] = {
    "15": [
        _summary("76", "group", GROUP_76, _path(SECTION_15, GROUP_76)),
        # The tariff has group 77, so the drill-down shows it. Opening it is what says it is
        # reserved — see `test_the_reserved_group_...` below.
        _summary("77", "group", GROUP_77, _path(SECTION_15, GROUP_77)),
    ],
    "16": [
        _summary("84", "group", GROUP_84, _path(SECTION_16, GROUP_84)),
        _summary("85", "group", GROUP_85, _path(SECTION_16, GROUP_85)),
    ],
}

CATEGORIES_BY_GROUP: dict[str, list[NodeSummary]] = {
    # Group 16 lives in section 04 and has nothing to do with section 16. This is the D19
    # collision, and it is why every collision test below uses these two.
    "16": [
        _summary("1602", "category", CAT_1602, _path(SECTION_04, GROUP_16, CAT_1602)),
        _summary("1604", "category", CAT_1604, _path(SECTION_04, GROUP_16, CAT_1604)),
    ],
    "76": [_summary("7601", "category", CAT_7601, _path(SECTION_15, GROUP_76, CAT_7601))],
    "77": [],  # the anomaly: no `children` key at all in the source file
    "85": [
        _summary("8501", "category", CAT_8501, _path(SECTION_16, GROUP_85, CAT_8501)),
        _summary("8517", "category", CAT_8517, _path(SECTION_16, GROUP_85, CAT_8517)),
    ],
}

PATH_8517 = [
    _step("section", "16", SECTION_16),
    _step("group", "85", GROUP_85),
    _step("category", "8517", CAT_8517),
]

TREE_8517 = CategoryTree(
    code="8517",
    level="category",
    description=CAT_8517,
    full_path=_path(SECTION_16, GROUP_85, CAT_8517),
    path=PATH_8517,
    nodes=[
        _leaf("8517110000", CORDED),
        _leaf("8517130000", SMARTPHONES),
        TreeNode(
            code="851769",
            description=OTHER,
            is_terminal=False,
            collapsed=False,
            children=[_leaf("8517691000", VIDEOPHONES), _leaf("8517692000", INTERCOMS)],
        ),
    ],
    terminal_count=4,
    truncated=False,
    is_dead_end=False,
)

TREE_851769 = CategoryTree(
    code="851769",
    level="code",
    description=OTHER,
    full_path=_path(SECTION_16, GROUP_85, CAT_8517, OTHER),
    path=[*PATH_8517, _step("code", "851769", OTHER)],
    nodes=[_leaf("8517691000", VIDEOPHONES), _leaf("8517692000", INTERCOMS)],
    terminal_count=2,
    truncated=False,
    is_dead_end=False,
)


def _big_branches() -> list[TreeNode]:
    """Heading 8501 — 76 children in the real file, and one of the payloads that overflow.

    Two levels, because the truncation the model has to recover from is exactly "there are
    children you cannot see from here".
    """
    branches: list[TreeNode] = []
    for index in range(30):
        code = f"8501{10 + index:02d}"
        branches.append(
            TreeNode(
                code=code,
                description=f"двигуни потужністю не більш як {index + 1} кВт",
                is_terminal=False,
                collapsed=False,
                children=[
                    _leaf(f"{code}{suffix}", text)
                    for suffix, text in (
                        ("1000", "для цивільної авіації"),
                        ("9100", "потужністю не більш як 750 Вт"),
                        ("9900", "інші"),
                    )
                ],
            )
        )
    return branches


BIG_BRANCHES = _big_branches()
FIRST_BRANCH = BIG_BRANCHES[0]
HIDDEN_LEAF = FIRST_BRANCH.children[0]

TREE_8501 = CategoryTree(
    code="8501",
    level="category",
    description=CAT_8501,
    full_path=_path(SECTION_16, GROUP_85, CAT_8501),
    path=[
        _step("section", "16", SECTION_16),
        _step("group", "85", GROUP_85),
        _step("category", "8501", CAT_8501),
    ],
    nodes=BIG_BRANCHES,
    terminal_count=90,
    truncated=False,
    is_dead_end=False,
)

TREE_FIRST_BRANCH = CategoryTree(
    code=FIRST_BRANCH.code,
    level="code",
    description=FIRST_BRANCH.description,
    full_path=_path(SECTION_16, GROUP_85, CAT_8501, FIRST_BRANCH.description),
    path=[*TREE_8501.path, _step("code", FIRST_BRANCH.code, FIRST_BRANCH.description)],
    nodes=list(FIRST_BRANCH.children),
    terminal_count=3,
    truncated=False,
    is_dead_end=False,
)

# `is_dead_end` is `not is_terminal and child_count == 0` (ingest.py). This snapshot has
# exactly one such node — group 77 — but the column is general, and a reserved heading must
# read as "reserved by construction" rather than as an empty answer.
TREE_7690 = CategoryTree(
    code="7690",
    level="category",
    description="Позиція зарезервована",
    full_path=_path(SECTION_15, GROUP_76, "Позиція зарезервована"),
    path=[
        _step("section", "15", SECTION_15),
        _step("group", "76", GROUP_76),
        _step("category", "7690", "Позиція зарезервована"),
    ],
    nodes=[],
    terminal_count=0,
    truncated=False,
    is_dead_end=True,
)

TREES: dict[tuple[str, str], CategoryTree] = {
    ("category", "8517"): TREE_8517,
    ("category", "8501"): TREE_8501,
    ("category", "7690"): TREE_7690,
    ("code", "851769"): TREE_851769,
    ("code", FIRST_BRANCH.code): TREE_FIRST_BRANCH,
}


def _detail(
    code: str,
    level: TariffLevel,
    description: str,
    full_path: str,
    *,
    terminal: bool = False,
    children: int = 0,
    ancestors: list[str] | None = None,
    ambiguous: bool = False,
    dead_end: bool = False,
) -> NodeDetail:
    return NodeDetail(
        code=code,
        level=level,
        description=description,
        full_path=full_path,
        is_terminal=terminal,
        depth=len(ancestors or []),
        child_count=children,
        ancestor_codes=ancestors or [],
        path_is_ambiguous=ambiguous,
        is_dead_end=dead_end,
    )


DETAILS: dict[tuple[str, str], NodeDetail] = {
    ("group", "16"): _detail(
        "16", "group", GROUP_16, _path(SECTION_04, GROUP_16), children=2, ancestors=["04"]
    ),
    ("group", "77"): _detail(
        "77", "group", GROUP_77, _path(SECTION_15, GROUP_77), ancestors=["15"], dead_end=True
    ),
    ("group", "85"): _detail(
        "85", "group", GROUP_85, _path(SECTION_16, GROUP_85), children=2, ancestors=["16"]
    ),
    ("category", "8517"): _detail(
        "8517",
        "category",
        CAT_8517,
        _path(SECTION_16, GROUP_85, CAT_8517),
        children=3,
        ancestors=["16", "85"],
    ),
    ("code", "8517130000"): _detail(
        "8517130000",
        "code",
        SMARTPHONES,
        _path(SECTION_16, GROUP_85, CAT_8517, SMARTPHONES),
        terminal=True,
        ancestors=["16", "85", "8517"],
    ),
    ("code", "8517692000"): _detail(
        "8517692000",
        "code",
        INTERCOMS,
        _path(SECTION_16, GROUP_85, CAT_8517, OTHER, INTERCOMS),
        terminal=True,
        ancestors=["16", "85", "8517", "851769"],
        ambiguous=True,
    ),
}

ANCESTRY: dict[tuple[str, str], list[NodeSummary]] = {
    # Keyed by the level the code resolves to, like `_ANCESTRY`'s `$2`. There is deliberately
    # no ("section", …) key: the ancestry query can never be asked about a section, because
    # the level always comes from the length of the code.
    ("group", "16"): [SEC_04_ROW],
    ("group", "76"): [SEC_15_ROW],
    ("group", "77"): [SEC_15_ROW],
    ("group", "85"): [SEC_16_ROW],
    ("category", "8501"): [SEC_16_ROW, CATEGORIES_BY_GROUP["85"][0]],
    ("category", "8517"): [SEC_16_ROW, CATEGORIES_BY_GROUP["85"][1]],
    ("code", "8517130000"): [
        SEC_16_ROW,
        _summary("85", "group", GROUP_85, _path(SECTION_16, GROUP_85)),
        _summary("8517", "category", CAT_8517, _path(SECTION_16, GROUP_85, CAT_8517)),
    ],
}

HITS = [
    _summary(
        "8517130000",
        "code",
        SMARTPHONES,
        _path(SECTION_16, GROUP_85, CAT_8517, SMARTPHONES),
        terminal=True,
    ),
    _summary(
        "8517110000",
        "code",
        CORDED,
        _path(SECTION_16, GROUP_85, CAT_8517, CORDED),
        terminal=True,
    ),
    _summary("8517", "category", CAT_8517, _path(SECTION_16, GROUP_85, CAT_8517)),
    _summary(
        "8517691000",
        "code",
        VIDEOPHONES,
        _path(SECTION_16, GROUP_85, CAT_8517, OTHER, VIDEOPHONES),
        terminal=True,
    ),
    _summary(
        "8517692000",
        "code",
        INTERCOMS,
        _path(SECTION_16, GROUP_85, CAT_8517, OTHER, INTERCOMS),
        terminal=True,
    ),
    _summary("85", "group", GROUP_85, _path(SECTION_16, GROUP_85)),
]


def _adaptive(tree: CategoryTree) -> CategoryTree:
    """`TariffRepo._tree`'s cut-off, imported rather than restated.

    The threshold and the collapse both come from `app.tariff.repo`, so a fixture payload
    truncates here for the same reason and at the same size as a real one.
    """
    if len(tree.model_dump_json(by_alias=True)) <= MAX_TREE_CHARS:
        return tree
    return tree.model_copy(update={"nodes": _collapse(tree.nodes), "truncated": True})


class _Repo:
    """Enough of `TariffRepo` to drive the six navigation tools.

    `calls` is the routing log the collision tests read: it records the LEVEL each lookup
    asked for as well as the code, which is the only way to show that a bare "16" went to the
    group rows and not the section ones.
    """

    def __init__(
        self,
        *,
        fail: Exception | None = None,
        ancestry_fails: bool = False,
        hits: list[NodeSummary] | None = None,
    ) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.fail = fail
        self.ancestry_fails = ancestry_fails
        self.hits = HITS if hits is None else hits

    def _maybe_fail(self) -> None:
        if self.fail is not None:
            raise self.fail

    async def list_groups_in_section(self, section_code: str) -> list[NodeSummary]:
        self.calls.append(("list_groups_in_section", "section", section_code))
        self._maybe_fail()
        return GROUPS_BY_SECTION.get(section_code, [])

    async def list_categories_in_group(self, group_code: str) -> list[NodeSummary]:
        self.calls.append(("list_categories_in_group", "group", group_code))
        self._maybe_fail()
        return CATEGORIES_BY_GROUP.get(group_code, [])

    async def open_category(self, category_code: str) -> CategoryTree:
        self.calls.append(("open_category", "category", category_code))
        self._maybe_fail()
        return self._tree("category", category_code)

    async def expand(self, prefix: str) -> CategoryTree:
        level = level_for_code(prefix)
        self.calls.append(("expand", level, prefix))
        self._maybe_fail()
        return self._tree(level, prefix)

    def _tree(self, level: str, code: str) -> CategoryTree:
        tree = TREES.get((level, code))
        if tree is None:
            raise UnknownCodeError(code, level)
        return _adaptive(tree)

    async def resolve(self, code: str) -> NodeDetail | None:
        level = level_for_code(code)  # the real rule: two digits is ALWAYS a group
        self.calls.append(("resolve", level, code))
        self._maybe_fail()
        return DETAILS.get((level, code))

    async def ancestry(self, code: str) -> list[NodeSummary]:
        level = level_for_code(code)
        self.calls.append(("ancestry", level, code))
        if self.ancestry_fails:
            raise RuntimeError("ancestry query failed")
        self._maybe_fail()
        return ANCESTRY.get((level, code), [])

    async def search_candidates(self, query: str, limit: int) -> list[NodeSummary]:
        self.calls.append(("search_candidates", query, limit))
        self._maybe_fail()
        return self.hits[:limit]


class _FakeStore:
    """Only the two id generators `AgentContext.generate_id` reaches."""

    def generate_thread_id(self, context: Any) -> str:
        return f"thr_{uuid.uuid4().hex}"

    def generate_item_id(self, item_type: str, thread: Any, context: Any) -> str:
        return f"{item_type[:3]}_{uuid.uuid4().hex[:12]}"


def _context(repo: Any = None) -> UktzedContext:
    thread = ThreadMetadata(id="thr_test", created_at=datetime.now(), status=ActiveStatus())
    return UktzedContext(
        thread=thread,
        store=_FakeStore(),
        request_context=RequestContext(user_id=7, request_id="test", locale="uk"),
        tariff=repo if repo is not None else _Repo(),
    )


def _wrap(agent_ctx: UktzedContext) -> RunContextWrapper[UktzedContext]:
    return RunContextWrapper(context=agent_ctx)


_ALL_FIXTURE_CODES = (
    {"04", "15", "16", "76", "77", "84", "85", "1602", "1604", "7601", "7690", "8501", "8517"}
    | {"851769", "8517110000", "8517130000", "8517691000", "8517692000"}
    | {branch.code for branch in BIG_BRANCHES}
    | {leaf.code for branch in BIG_BRANCHES for leaf in branch.children}
)


def _codes(agent_ctx: UktzedContext) -> set[str]:
    """Every code the ledger recorded this turn, read back through `evidence_for`."""
    seen = {code for code in _ALL_FIXTURE_CODES if agent_ctx.ledger.evidence_for(code) is not None}
    assert len(seen) == agent_ctx.ledger.codes_seen, "a code outside the fixture was recorded"
    return seen


def _tasks(agent_ctx: UktzedContext) -> list[Any]:
    """The live drill-down the user watches, as the client would receive it."""
    item = agent_ctx.workflow_item
    return list(item.workflow.tasks) if item else []


def _trail_text(path: list[PathStep]) -> str:
    return " › ".join(step.description for step in path)


# name, tool, kwargs, the digest the trace must carry
_GOOD_CALLS = [
    ("list_groups_in_section", list_groups_in_section, {"section_code": "16"}, "groups:2"),
    ("list_categories_in_group", list_categories_in_group, {"group_code": "85"}, "categories:2"),
    ("open_category", open_category, {"category_code": "8517"}, "tree:4:truncated=False"),
    ("expand", expand, {"prefix": "851769"}, "tree:2:truncated=False"),
    ("search_candidates", search_candidates, {"query": "смартфон", "limit": 5}, "candidates:5"),
    ("resolve_code", resolve_code, {"code": "8517130000"}, "resolve:code:terminal=True"),
]
_GOOD_IDS = [name for name, _, _, _ in _GOOD_CALLS]

# name, tool, kwargs, the error the trace must carry. Every one is an argument the model can
# plausibly produce: a group number where a section number goes, a heading where a group goes,
# a group where a heading goes, a leaf where a prefix goes, blank text, a truncated code.
_BAD_CALLS = [
    ("list_groups_in_section", list_groups_in_section, {"section_code": "77"}, "bad_section_code"),
    (
        "list_categories_in_group",
        list_categories_in_group,
        {"group_code": "8517"},
        "bad_group_code",
    ),
    ("open_category", open_category, {"category_code": "85"}, "bad_code"),
    ("expand", expand, {"prefix": "8517130000"}, "bad_code"),
    ("search_candidates", search_candidates, {"query": "   ", "limit": 5}, "empty_query"),
    ("resolve_code", resolve_code, {"code": "851"}, "bad_code"),
]
_BAD_IDS = [name for name, _, _, _ in _BAD_CALLS]


# --------------------------------------------------------------------------------------
# What the tools record — the gate's input
# --------------------------------------------------------------------------------------


async def test_listing_the_groups_of_a_section_records_every_group_it_showed() -> None:
    agent_ctx = _context()

    result = await list_groups_in_section.__wrapped__(_wrap(agent_ctx), section_code="16")

    assert [node.code for node in result.nodes] == ["84", "85"]
    assert _codes(agent_ctx) == {"84", "85"}
    evidence = agent_ctx.ledger.evidence_for("85")
    assert evidence is not None
    assert evidence.description == GROUP_85
    assert evidence.full_path == _path(SECTION_16, GROUP_85)
    assert evidence.is_terminal is False
    assert evidence.tool_call_index == 0
    assert result.explanation == _EXPLAIN_GROUPS


async def test_listing_the_categories_of_a_group_records_every_category_it_showed() -> None:
    agent_ctx = _context()

    result = await list_categories_in_group.__wrapped__(_wrap(agent_ctx), group_code="85")

    assert [node.code for node in result.nodes] == ["8501", "8517"]
    assert _codes(agent_ctx) == {"8501", "8517"}
    evidence = agent_ctx.ledger.evidence_for("8517")
    assert evidence is not None
    assert evidence.description == CAT_8517
    assert evidence.full_path == _path(SECTION_16, GROUP_85, CAT_8517)
    assert result.path == [_step("section", "16", SECTION_16)]
    assert result.explanation == _EXPLAIN_CATEGORIES


async def test_opening_a_category_records_the_heading_and_every_code_under_it() -> None:
    """Including the intermediate 6-digit node: the model can see it, so it is evidence, and
    `emit_classification` needs its `is_terminal=False` to reject it as an answer."""
    agent_ctx = _context()

    result = await open_category.__wrapped__(_wrap(agent_ctx), category_code="8517")

    assert result.tree is not None
    assert _codes(agent_ctx) == {
        "8517",
        "8517110000",
        "8517130000",
        "851769",
        "8517691000",
        "8517692000",
    }
    leaf = agent_ctx.ledger.evidence_for("8517130000")
    assert leaf is not None
    assert leaf.description == SMARTPHONES
    assert leaf.is_terminal is True
    # Rebuilt code-side by `iter_tree_paths`, and it has to match the stored string exactly —
    # the answer is rendered from it, and 2,473 leaf descriptions are literally "інші".
    assert leaf.full_path == _path(SECTION_16, GROUP_85, CAT_8517, SMARTPHONES)
    nested = agent_ctx.ledger.evidence_for("8517692000")
    assert nested is not None
    assert nested.full_path == _path(SECTION_16, GROUP_85, CAT_8517, OTHER, INTERCOMS)
    intermediate = agent_ctx.ledger.evidence_for("851769")
    assert intermediate is not None
    assert intermediate.is_terminal is False


async def test_search_records_every_hypothesis_it_showed() -> None:
    agent_ctx = _context()

    result = await search_candidates.__wrapped__(_wrap(agent_ctx), query="смартфон", limit=6)

    assert [hit.code for hit in result.search.candidates] == [hit.code for hit in HITS]
    assert _codes(agent_ctx) == {hit.code for hit in HITS}
    evidence = agent_ctx.ledger.evidence_for("8517130000")
    assert evidence is not None
    assert evidence.full_path == _path(SECTION_16, GROUP_85, CAT_8517, SMARTPHONES)
    assert result.explanation == _EXPLAIN_CANDIDATES


async def test_resolving_a_code_records_the_one_row_it_showed() -> None:
    agent_ctx = _context()

    result = await resolve_code.__wrapped__(_wrap(agent_ctx), code="8517130000")

    assert result.node is not None
    assert _codes(agent_ctx) == {"8517130000"}
    evidence = agent_ctx.ledger.evidence_for("8517130000")
    assert evidence is not None
    assert evidence.description == SMARTPHONES
    assert evidence.full_path == _path(SECTION_16, GROUP_85, CAT_8517, SMARTPHONES)
    assert evidence.is_terminal is True
    assert [step.code for step in result.path] == ["16", "85", "8517"]
    assert result.explanation == _EXPLAIN_TERMINAL


async def test_a_code_open_category_recorded_is_a_code_the_gate_accepts() -> None:
    """The two halves joined up: the tool feeds the ledger, the ledger is what E4 reads.

    `tests/test_gate.py` proves the gate against a hand-built ledger; this proves the tool
    actually builds that ledger, which is the half a hand-built one cannot check.
    """
    agent_ctx = _context()
    await open_category.__wrapped__(_wrap(agent_ctx), category_code="8517")

    ack = await emit_classification.__wrapped__(
        _wrap(agent_ctx),
        codes=[ClassifiedCode(code="8517130000", full_description="смартфон")],
        confidence="висока",
        rationale="Апарат для стільникового зв'язку з сенсорним екраном.",
        path=[],
        alternatives=[],
        product_summary="Смартфон.",
        evidence="опис користувача",
        label="Смартфони",
    )

    assert ack.ok is True, ack.message
    assert agent_ctx.emitted_codes == ["8517130000"]


# --------------------------------------------------------------------------------------
# The tool trace: every call is bracketed, including the error paths
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(("name", "tool", "kwargs", "digest"), _GOOD_CALLS, ids=_GOOD_IDS)
async def test_every_tool_opens_and_closes_exactly_one_ledger_call(
    name: str, tool: Any, kwargs: dict[str, Any], digest: str
) -> None:
    """An unclosed call is an unreadable trace and, for `open_category`, an unusable
    `_navigated_prefixes` entry — the gate reads the call log, not just the codes."""
    agent_ctx = _context()

    await tool.__wrapped__(_wrap(agent_ctx), **kwargs)

    (record,) = agent_ctx.ledger.calls
    assert record.tool_name == name
    assert record.index == 0
    assert record.duration_ms is not None, "begin_call without end_call"
    assert record.result_digest == digest
    assert record.error is None


@pytest.mark.parametrize(("name", "tool", "kwargs", "digest"), _GOOD_CALLS, ids=_GOOD_IDS)
async def test_a_repo_failure_still_closes_the_call_and_returns_an_envelope(
    name: str, tool: Any, kwargs: dict[str, Any], digest: str
) -> None:
    """A raised exception becomes an error STRING by the SDK's default failure handler, which
    then fails `output_type` validation and kills the run. Nothing here may raise."""
    agent_ctx = _context(_Repo(fail=RuntimeError("connection lost")))

    result = await tool.__wrapped__(_wrap(agent_ctx), **kwargs)

    assert result.error is not None
    (record,) = agent_ctx.ledger.calls
    assert record.tool_name == name
    assert record.error is not None
    assert "RuntimeError" in record.error
    assert record.duration_ms is not None, "the error path skipped end_call"
    assert record.result_digest is None
    assert agent_ctx.ledger.codes_seen == 0


@pytest.mark.parametrize(("name", "tool", "kwargs", "error"), _BAD_CALLS, ids=_BAD_IDS)
async def test_a_malformed_argument_is_an_answer_the_model_can_act_on(
    name: str, tool: Any, kwargs: dict[str, Any], error: str
) -> None:
    """Structured, Ukrainian, and cheap: the argument is rejected before the database is
    touched, and the model is told to fix it rather than being handed an exception."""
    repo = _Repo()
    agent_ctx = _context(repo)

    result = await tool.__wrapped__(_wrap(agent_ctx), **kwargs)

    assert result.error, "a malformed argument must come back as an error field"
    assert result.explanation == _EXPLAIN_RETRY
    assert repo.calls == [], "the repo was queried with an argument that never validated"
    (record,) = agent_ctx.ledger.calls
    assert record.tool_name == name
    assert record.error == error
    assert record.duration_ms is not None
    assert agent_ctx.ledger.codes_seen == 0


async def test_an_unknown_code_keeps_the_breadcrumb_it_was_lost_in() -> None:
    """P5: the one result where the model is already off-track is the one that must say where
    it is. v1 dropped the breadcrumb from exactly this result."""
    agent_ctx = _context()

    result = await open_category.__wrapped__(_wrap(agent_ctx), category_code="8599")

    assert result.tree is None
    assert "8599" in (result.error or "")
    assert SECTION_16 in result.explanation, "the branch the model is standing in"
    assert 'list_categories_in_group("85")' in result.explanation
    assert agent_ctx.ledger.calls[0].error == "not_found"
    assert agent_ctx.ledger.calls[0].duration_ms is not None
    assert agent_ctx.ledger.codes_seen == 0


async def test_a_code_missing_from_the_dataset_is_not_recorded_as_evidence() -> None:
    agent_ctx = _context()

    result = await resolve_code.__wrapped__(_wrap(agent_ctx), code="8517999999")

    assert result.node is None
    assert "8517999999" in (result.error or "")
    assert agent_ctx.ledger.codes_seen == 0, "a code that does not exist became emittable"
    assert agent_ctx.ledger.calls[0].error == "not_found"


async def test_a_broken_breadcrumb_does_not_lose_the_result_it_decorates() -> None:
    """`_breadcrumb` swallows everything on purpose: losing a decoration must not lose the
    codes, and if the database is really down the main query has already failed."""
    agent_ctx = _context(_Repo(ancestry_fails=True))

    result = await list_categories_in_group.__wrapped__(_wrap(agent_ctx), group_code="85")

    assert result.path == []
    assert [node.code for node in result.nodes] == ["8501", "8517"]
    assert result.error is None
    assert agent_ctx.ledger.calls[0].result_digest == "categories:2"
    assert agent_ctx.ledger.codes_seen == 2


# --------------------------------------------------------------------------------------
# D19 — sections are not addressable. All 21 section codes are also group codes.
# --------------------------------------------------------------------------------------


async def test_a_bare_two_digit_code_is_a_group_in_every_tool_that_takes_a_code() -> None:
    """The code 16 is section «Машини, обладнання та механізми» AND group «Готові харчові
    продукти з м'яса». v1 pushed both through one `section=` argument and silently returned
    the wrong subtree; here only the parameter literally named `section_code` is a section."""
    repo = _Repo()
    agent_ctx = _context(repo)

    listed = await list_categories_in_group.__wrapped__(_wrap(agent_ctx), group_code="16")
    resolved = await resolve_code.__wrapped__(_wrap(agent_ctx), code="16")

    assert [node.code for node in listed.nodes] == ["1602", "1604"]
    assert all(node.code.startswith("16") for node in listed.nodes)
    assert resolved.node is not None
    assert resolved.node.level == "group"
    assert resolved.node.description == GROUP_16
    assert SECTION_16 not in resolved.node.full_path
    # Both lookups asked for group rows. Neither tool can reach a section row at all.
    assert [(call[0], call[1]) for call in repo.calls] == [
        ("list_categories_in_group", "group"),
        ("ancestry", "group"),
        ("resolve", "group"),
        ("ancestry", "group"),
    ]


async def test_the_section_tool_and_the_group_tool_return_different_subtrees_for_16() -> None:
    """The collision, from both sides at once, in one turn."""
    agent_ctx = _context()

    groups = await list_groups_in_section.__wrapped__(_wrap(agent_ctx), section_code="16")
    categories = await list_categories_in_group.__wrapped__(_wrap(agent_ctx), group_code="16")

    assert [node.code for node in groups.nodes] == ["84", "85"]
    assert [node.code for node in categories.nodes] == ["1602", "1604"]
    assert groups.level == "section"
    assert categories.level == "group"
    assert _codes(agent_ctx) == {"84", "85", "1602", "1604"}


async def test_a_group_number_is_not_a_section_number() -> None:
    """Group 77 exists; section 77 does not. The section tool rejects everything outside
    01-21 instead of quietly listing a group's categories as if they were groups."""
    repo = _Repo()
    agent_ctx = _context(repo)

    result = await list_groups_in_section.__wrapped__(_wrap(agent_ctx), section_code="77")

    assert result.nodes == []
    assert "77" in (result.error or "")
    assert "01" in (result.error or "") and "21" in (result.error or "")
    assert repo.calls == []
    assert agent_ctx.ledger.calls[0].error == "bad_section_code"


@pytest.mark.parametrize("section_code", ["00", "22", "99", "8", "016", "1a"])
async def test_the_section_tool_takes_nothing_but_01_to_21(section_code: str) -> None:
    repo = _Repo()
    agent_ctx = _context(repo)

    result = await list_groups_in_section.__wrapped__(_wrap(agent_ctx), section_code=section_code)

    assert result.error is not None
    assert repo.calls == []


@pytest.mark.parametrize(
    ("tool", "kwargs"),
    [(open_category, {"category_code": "16"}), (expand, {"prefix": "16"})],
    ids=["open_category", "expand"],
)
async def test_the_drill_down_tools_refuse_a_two_digit_code_outright(
    tool: Any, kwargs: dict[str, Any]
) -> None:
    """Neither a section nor a group is openable: the tree starts at the 4-digit heading, so a
    2-digit argument can only be a level confusion and is never guessed at."""
    repo = _Repo()
    agent_ctx = _context(repo)

    result = await tool.__wrapped__(_wrap(agent_ctx), **kwargs)

    assert result.tree is None
    assert "16" in (result.error or "")
    assert repo.calls == []


async def test_the_section_listing_does_not_borrow_the_groups_breadcrumb() -> None:
    """D19 surviving in a decoration, found by this test and fixed.

    `repo.ancestry` derives the level from the code's LENGTH, and two digits is always a
    GROUP — so a SECTION code was looked up as a group and section 16's listing came back
    carrying group 16's parent, section 04 «Готові харчові продукти». Confirmed against the
    real dataset. A section has no ancestors, so the tool now sends none. Invisible for
    sections 01-05, whose numbers happen to name groups inside themselves, which is exactly
    why a test rather than a glance was needed.
    """
    agent_ctx = _context()

    result = await list_groups_in_section.__wrapped__(_wrap(agent_ctx), section_code="16")

    assert result.path == []
    assert SECTION_04 not in _trail_text(result.path)


# --------------------------------------------------------------------------------------
# The adaptive cut-off
# --------------------------------------------------------------------------------------


async def test_a_small_heading_comes_back_whole() -> None:
    """~90% of headings fit under `MAX_TREE_CHARS`; for those the truncation warning would be
    a lie and an extra `expand` round-trip."""
    agent_ctx = _context()

    result = await open_category.__wrapped__(_wrap(agent_ctx), category_code="8517")

    assert result.tree is not None
    assert result.tree.truncated is False
    assert result.explanation == _EXPLAIN_TERMINAL
    assert _EXPLAIN_TRUNCATED not in result.explanation
    assert agent_ctx.ledger.calls[0].result_digest == "tree:4:truncated=False"


async def test_a_heading_over_the_cut_off_is_truncated_and_says_so() -> None:
    agent_ctx = _context()

    result = await open_category.__wrapped__(_wrap(agent_ctx), category_code="8501")

    assert result.tree is not None
    assert len(TREE_8501.model_dump_json(by_alias=True)) > MAX_TREE_CHARS, "fixture too small"
    assert result.tree.truncated is True
    assert result.explanation == f"{_EXPLAIN_TERMINAL} {_EXPLAIN_TRUNCATED}"
    assert "expand" in result.explanation
    # The escape hatch has to be visible on the nodes themselves, not only in the prose.
    assert all(node.collapsed and node.children == [] for node in result.tree.nodes)
    # The count is of the WHOLE subtree, hidden nodes included: it is how the model knows
    # what it is not being shown.
    assert result.tree.terminal_count == 90
    assert agent_ctx.ledger.calls[0].result_digest == "tree:90:truncated=True"


async def test_a_truncated_payload_records_what_it_showed_and_nothing_more() -> None:
    """The ledger is a record of what the MODEL SAW. Recording the hidden leaves would make
    codes emittable that never appeared in any tool result — the gate inverted."""
    agent_ctx = _context()

    await open_category.__wrapped__(_wrap(agent_ctx), category_code="8501")

    seen = _codes(agent_ctx)
    assert "8501" in seen
    assert {branch.code for branch in BIG_BRANCHES} <= seen
    assert HIDDEN_LEAF.code not in seen
    assert agent_ctx.ledger.evidence_for(HIDDEN_LEAF.code) is None
    assert agent_ctx.ledger.codes_seen == len(BIG_BRANCHES) + 1


async def test_expand_goes_deeper_into_a_prefix_and_records_the_new_codes() -> None:
    """The way out of a truncated payload, and the second half of the walk the gate checks."""
    agent_ctx = _context()
    await open_category.__wrapped__(_wrap(agent_ctx), category_code="8501")
    before = agent_ctx.ledger.codes_seen

    result = await expand.__wrapped__(_wrap(agent_ctx), prefix=FIRST_BRANCH.code)

    assert result.tree is not None
    assert [node.code for node in result.tree.nodes] == [
        leaf.code for leaf in FIRST_BRANCH.children
    ]
    assert agent_ctx.ledger.codes_seen == before + len(FIRST_BRANCH.children)
    evidence = agent_ctx.ledger.evidence_for(HIDDEN_LEAF.code)
    assert evidence is not None
    assert evidence.is_terminal is True
    assert evidence.tool_call_index == 1, "evidence must point at the call that surfaced it"
    assert evidence.full_path == _path(
        SECTION_16, GROUP_85, CAT_8501, FIRST_BRANCH.description, HIDDEN_LEAF.description
    )
    # First sighting wins, so the branch itself still belongs to the open_category call.
    branch = agent_ctx.ledger.evidence_for(FIRST_BRANCH.code)
    assert branch is not None
    assert branch.tool_call_index == 0


# --------------------------------------------------------------------------------------
# Search: the cap, and the hypothesis framing
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("asked", "capped"),
    [
        (3, 3),
        (MAX_SEARCH_LIMIT, MAX_SEARCH_LIMIT),
        (100, MAX_SEARCH_LIMIT),
        (0, 10),  # 0 is "the model did not really choose"; the default is 10, not none
        (-5, 1),
    ],
)
async def test_search_never_asks_the_database_for_more_than_the_cap(
    asked: int, capped: int
) -> None:
    repo = _Repo()
    agent_ctx = _context(repo)

    await search_candidates.__wrapped__(_wrap(agent_ctx), query="смартфон", limit=asked)

    assert repo.calls == [("search_candidates", "смартфон", capped)]
    # The trace records the limit that was USED, not the one that was asked for.
    assert agent_ctx.ledger.calls[0].args == {"query": "смартфон", "limit": capped}


async def test_search_records_exactly_the_hits_it_returned() -> None:
    agent_ctx = _context()

    result = await search_candidates.__wrapped__(_wrap(agent_ctx), query="смартфон", limit=3)

    assert len(result.search.candidates) == 3
    assert _codes(agent_ctx) == {hit.code for hit in HITS[:3]}
    assert agent_ctx.ledger.codes_seen == 3
    assert agent_ctx.ledger.calls[0].result_digest == "candidates:3"


async def test_search_with_no_hits_is_an_answer_rather_than_an_error() -> None:
    """Nothing found is a normal outcome, not a failure: the tariff writes «машини
    обчислювальні портативні» where the user writes «ноутбук», so the tool spends the result
    on telling the model how to ask again."""
    agent_ctx = _context(_Repo(hits=[]))

    result = await search_candidates.__wrapped__(_wrap(agent_ctx), query="ноутбук", limit=5)

    assert result.search.candidates == []
    assert result.error is None
    assert result.explanation == _EXPLAIN_NO_CANDIDATES
    assert agent_ctx.ledger.calls[0].result_digest == "candidates:0"
    assert agent_ctx.ledger.calls[0].error is None
    assert agent_ctx.ledger.codes_seen == 0


async def test_a_normalised_query_is_what_reaches_the_database_and_the_trace() -> None:
    repo = _Repo()
    agent_ctx = _context(repo)

    result = await search_candidates.__wrapped__(
        _wrap(agent_ctx), query="  смартфон   з   титановим корпусом ", limit=2
    )

    assert result.search.query == "смартфон з титановим корпусом"
    assert repo.calls[0][1] == "смартфон з титановим корпусом"


# --------------------------------------------------------------------------------------
# Group 15/77 — the dataset's one real anomaly
# --------------------------------------------------------------------------------------


async def test_the_reserved_group_77_comes_back_empty_instead_of_raising() -> None:
    """Group 77 has no `children` key at all in the source file. An empty list on its own
    invites a retry loop, so the tool has to say which of the two things happened."""
    agent_ctx = _context()

    result = await list_categories_in_group.__wrapped__(_wrap(agent_ctx), group_code="77")

    assert result.nodes == []
    assert "77" in (result.error or "")
    assert result.explanation == _EXPLAIN_EMPTY_GROUP
    assert "resolve_code" in result.explanation
    # A successful call, not a failed one: the query ran and the answer is "nothing here".
    assert agent_ctx.ledger.calls[0].result_digest == "categories:0"
    assert agent_ctx.ledger.calls[0].error is None
    assert agent_ctx.ledger.codes_seen == 0


async def test_section_15_still_lists_the_reserved_group() -> None:
    """The tariff has group 77, so the drill-down shows it; hiding it would make the listing
    disagree with the printed nomenclature."""
    agent_ctx = _context()

    result = await list_groups_in_section.__wrapped__(_wrap(agent_ctx), section_code="15")

    assert [node.code for node in result.nodes] == ["76", "77"]
    assert _codes(agent_ctx) == {"76", "77"}


async def test_resolving_the_reserved_group_says_reserved_not_missing() -> None:
    agent_ctx = _context()

    result = await resolve_code.__wrapped__(_wrap(agent_ctx), code="77")

    assert result.node is not None
    assert result.node.is_dead_end is True
    assert result.explanation == _EXPLAIN_DEAD_END
    assert result.error is None


async def test_a_reserved_heading_reads_as_reserved_rather_than_as_an_empty_tree() -> None:
    """`is_dead_end` is `not is_terminal and child_count == 0`, which is a general rule; this
    snapshot happens to contain exactly one such node. A reserved branch must never look like
    a heading whose codes failed to load."""
    agent_ctx = _context()

    result = await open_category.__wrapped__(_wrap(agent_ctx), category_code="7690")

    assert result.tree is not None
    assert result.tree.nodes == []
    assert result.explanation == _EXPLAIN_DEAD_END
    assert result.error is None
    assert agent_ctx.ledger.codes_seen == 1  # the heading itself, and nothing under it


# --------------------------------------------------------------------------------------
# What the model is told next, and what the user watches happen
# --------------------------------------------------------------------------------------


async def test_an_intermediate_code_is_never_offered_as_an_answer() -> None:
    agent_ctx = _context()

    result = await resolve_code.__wrapped__(_wrap(agent_ctx), code="8517")

    assert result.node is not None
    assert result.node.is_terminal is False
    assert "expand" in result.explanation
    assert "3 нащадків" in result.explanation, "how many children it has to choose between"
    assert result.explanation != _EXPLAIN_TERMINAL


async def test_a_code_whose_text_path_collides_with_a_sibling_says_so() -> None:
    """23.71% of terminals share a path with a sibling because this snapshot dropped the
    header text that separates them. The honest answer shows the set, so the tool must not
    let that code pass as an ordinary terminal."""
    agent_ctx = _context()

    result = await resolve_code.__wrapped__(_wrap(agent_ctx), code="8517692000")

    assert result.node is not None
    assert result.node.path_is_ambiguous is True
    assert result.explanation == _EXPLAIN_AMBIGUOUS


@pytest.mark.parametrize("raw", ["8517 13 00 00", "8517.13.00.00", " 8517-13-00-00 "])
async def test_a_printed_code_is_normalised_before_anything_looks_at_it(raw: str) -> None:
    """`XXXX XX XX XX` is how the tariff is PRINTED, so a formatted code is a formatting
    question. It also has to be normalised in the trace: `_navigated_prefixes` matches the
    recorded argument against the emitted code, character for character."""
    repo = _Repo()
    agent_ctx = _context(repo)

    result = await resolve_code.__wrapped__(_wrap(agent_ctx), code=raw)

    assert result.node is not None
    assert result.node.code == "8517130000"
    assert agent_ctx.ledger.calls[0].args == {"code": "8517130000"}
    assert repo.calls[0] == ("resolve", "code", "8517130000")


async def test_each_call_streams_its_own_step_of_the_drill_down() -> None:
    """`add_workflow_task` computes the streamed index with `list.index()` and pydantic models
    compare by value, so two identical tasks would update task 0 instead of appending a second
    one and the user would watch one step of a four-step walk."""
    agent_ctx = _context()

    await list_categories_in_group.__wrapped__(_wrap(agent_ctx), group_code="85")
    await open_category.__wrapped__(_wrap(agent_ctx), category_code="8517")
    await open_category.__wrapped__(_wrap(agent_ctx), category_code="8501")

    tasks = _tasks(agent_ctx)
    assert len(tasks) == 3
    assert len({task.content for task in tasks}) == 3
    assert "8517" in tasks[1].title
    assert all(task.status_indicator == "complete" for task in tasks)


async def test_a_failed_call_streams_nothing_and_leaves_the_walk_visible() -> None:
    """A step the user watches is a step that HAPPENED. The error goes to the model in the
    envelope; a task saying «Позиція 8599» would show a walk into a branch that does not
    exist."""
    agent_ctx = _context()

    await open_category.__wrapped__(_wrap(agent_ctx), category_code="8517")
    await open_category.__wrapped__(_wrap(agent_ctx), category_code="8599")

    assert len(_tasks(agent_ctx)) == 1
    assert len(agent_ctx.ledger.calls) == 2, "both calls are in the trace, one of them failed"


async def test_one_turn_of_walking_accumulates_into_one_ledger() -> None:
    """Four calls, four bracketed records, one candidate set — the shape `emit_classification`
    reads when it asks whether the model actually walked to the code it is emitting."""
    agent_ctx = _context()

    await list_groups_in_section.__wrapped__(_wrap(agent_ctx), section_code="16")
    await list_categories_in_group.__wrapped__(_wrap(agent_ctx), group_code="85")
    await open_category.__wrapped__(_wrap(agent_ctx), category_code="8517")
    await expand.__wrapped__(_wrap(agent_ctx), prefix="851769")

    ledger = agent_ctx.ledger
    assert [call.tool_name for call in ledger.calls] == [
        "list_groups_in_section",
        "list_categories_in_group",
        "open_category",
        "expand",
    ]
    assert [call.index for call in ledger.calls] == [0, 1, 2, 3]
    assert all(call.duration_ms is not None and call.error is None for call in ledger.calls)
    assert _codes(agent_ctx) == {
        "84",
        "85",
        "8501",
        "8517",
        "8517110000",
        "8517130000",
        "851769",
        "8517691000",
        "8517692000",
    }
    ledger.reset()
    assert ledger.codes_seen == 0 and ledger.calls == []
