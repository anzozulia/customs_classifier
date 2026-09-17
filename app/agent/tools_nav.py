
# the en dash are correct typography here, not Latin look-alikes; pyproject pins ruff's
# defaults (no allowed-confusables), so the exemption is declared per file.
"""Navigation tools — the tariff tree walk, one tool per level, plus search and resolve.

Sections are NOT addressable (D19). All 21 section codes are also group codes, so a bare
2-digit code always means GROUP; `list_groups_in_section` is the only tool that takes a section
code and its parameter name says so. v1 pushed both through one `section=` argument and
silently returned the wrong subtree.

Every tool does the same five things, in this order:

1. `ledger.begin_call(name, args)`
2. ask the repo
3. `ledger.record_codes([...])` — everything it is about to put in front of the model
4. `add_workflow_task(CustomTask(...))` — the live drill-down the user watches happen
5. `ledger.end_call(rec, digest=… | error=…)`

Step 3 is what makes `emit_classification`'s gate real: a code is emittable only if some tool
in THIS turn recorded it here. Step 4 is the fix for v1's worst UX failure — 48.2s of median
cold latency behind one static string, with only 7.5% of turns finishing under ten seconds.

**Tools never raise.** A raised exception is turned into an error *string* by the SDK's default
failure handler, which then fails `output_type` validation and kills the run. Every failure
comes back as an envelope with the breadcrumb RETAINED — v1 dropped the breadcrumb from exactly
the one result where the model was already off-track.

**Why envelopes wrap the repo's payloads instead of replacing them.** `app.agent.schemas` owns
the shapes, and every tool result also has to restate the answer contract in `пояснення` at the
moment the model is looking at codes — v1's `tools.py:331` pattern, which its own post-mortem
called the cheapest and most reliable reinforcement in the project. Wrapping keeps one
definition of `CategoryTree` and adds the one field that is about the *contract* rather than
about the data.
"""

from __future__ import annotations

import re

from agents import function_tool
from chatkit.types import CustomTask
from pydantic import Field

from app.agent import PathStep, UAModel
from app.agent.context import Ctx
from app.agent.provenance import CodeEvidence, ToolCallRecord, TurnLedger
from app.agent.schemas import (
    CandidateList,
    CategoryTree,
    NodeDetail,
    NodeSummary,
    iter_tree_paths,
)
from app.tariff import TariffLevel
from app.tariff.repo import TariffRepo, UnknownCodeError, normalize_code

__all__ = [
    "NAVIGATION_TOOLS",
    "CategoryResult",
    "NodeListResult",
    "ResolveResult",
    "SearchResult",
    "expand",
    "list_categories_in_group",
    "list_groups_in_section",
    "open_category",
    "resolve_code",
    "search_candidates",
]

_SECTION_RE = re.compile(r"^(0[1-9]|1[0-9]|2[01])$")
_GROUP_RE = re.compile(r"^\d{2}$")
_CATEGORY_RE = re.compile(r"^\d{4}$")
_PREFIX_RE = re.compile(r"^\d{4}(\d{2})?(\d{2})?$")
_CODE_RE = re.compile(r"^(\d{2}|\d{4}|\d{6}|\d{8}|\d{10})$")

MAX_SEARCH_LIMIT = 25

# ---------------------------------------------------------------------------
# P5 — the answer contract, restated where the model is reading data.
# ---------------------------------------------------------------------------

_EXPLAIN_TERMINAL = (
    'Кінцеві коди (10 цифр, "термінальний": true) — для класифікації. '
    'Коди з "термінальний": false проміжні: це ще не відповідь, відкрий їх через expand. '
    "Перед видачею коду перевір, що всі ознаки товару узгоджуються з його повним описом."
)
_EXPLAIN_TRUNCATED = (
    "Гілка велика, показано не всі рівні: вузли з \"згорнуто\": true мають нащадків, яких тут "
    "немає. Щоб побачити їх, виклич expand з кодом такого вузла."
)
_EXPLAIN_DEAD_END = (
    "Ця гілка порожня за побудовою (зарезервована). Це не помилка довідника. "
    "Повернись на рівень вище і перевір іншу гілку."
)
_EXPLAIN_GROUPS = (
    "Це групи розділу. Обери групу і виклич list_categories_in_group з її кодом."
)
_EXPLAIN_CATEGORIES = (
    "Це товарні позиції (4 цифри) групи. Обери позицію і виклич open_category з її кодом."
)
_EXPLAIN_EMPTY_GROUP = (
    "У цій групі немає позицій — або коду групи немає в довіднику, або група зарезервована "
    "(як 77). Перевір код через resolve_code і повернись на рівень вище."
)
_EXPLAIN_CANDIDATES = (
    "Це ГІПОТЕЗИ з пошуку за текстом, а не відповідь. Перед видачею коду відкрий відповідну "
    "позицію через open_category і переконайся сам."
)
_EXPLAIN_NO_CANDIDATES = (
    "Нічого не знайдено. Довідник пише «машини обчислювальні портативні» там, де користувач "
    "пише «ноутбук», тож переформулюй запит мовою довідника або йди від каталогу розділів."
)
_EXPLAIN_AMBIGUOUS = (
    "Текстовий шлях цього коду збігається з сусідніми — розрізнити їх за описом неможливо. "
    "Покажи користувачу всі такі варіанти разом."
)
_EXPLAIN_RETRY = "Виправ код і виклич знову."
_EXPLAIN_UNAVAILABLE = (
    "Довідник тимчасово недоступний. Не вигадуй код: скажи користувачу, що довідник недоступний."
)


# ---------------------------------------------------------------------------
# Envelopes. The payload comes from app.agent.schemas; these add the contract line and
# the error branch, and nothing else.
# ---------------------------------------------------------------------------


class NodeListResult(UAModel):
    """Groups of a section, or categories of a group."""

    level: TariffLevel = Field(alias="рівень")
    path: list[PathStep] = Field(alias="шлях")
    nodes: list[NodeSummary] = Field(alias="коди")
    error: str | None = Field(default=None, alias="помилка")
    explanation: str = Field(alias="пояснення")


class CategoryResult(UAModel):
    """`open_category` / `expand`. `tree` is null only on the error branch.

    No breadcrumb field of its own: `CategoryTree.path` already carries it, and a second copy
    would be four long descriptions repeated in every payload. On the error branch there is no
    tree, so the breadcrumb goes into `пояснення` as text — which is the point of P5's rule
    that an error result keeps its breadcrumb, since that is the one result where the model is
    already off-track.
    """

    tree: CategoryTree | None = Field(default=None, alias="позиція")
    error: str | None = Field(default=None, alias="помилка")
    explanation: str = Field(alias="пояснення")


class SearchResult(UAModel):
    search: CandidateList = Field(alias="пошук")
    error: str | None = Field(default=None, alias="помилка")
    explanation: str = Field(alias="пояснення")


class ResolveResult(UAModel):
    node: NodeDetail | None = Field(default=None, alias="код")
    path: list[PathStep] = Field(alias="шлях")
    error: str | None = Field(default=None, alias="помилка")
    explanation: str = Field(alias="пояснення")


# ---------------------------------------------------------------------------
# Ledger plumbing
# ---------------------------------------------------------------------------


def _begin(
    ctx: Ctx, name: str, args: dict[str, object]
) -> tuple[TurnLedger, ToolCallRecord, TariffRepo]:
    ledger = ctx.context.ledger
    return ledger, ledger.begin_call(name, args), ctx.context.tariff


def _record_summaries(ledger: TurnLedger, rows: list[NodeSummary], call_index: int) -> None:
    ledger.record_codes(
        [
            CodeEvidence(
                code=row.code,
                description=row.description,
                full_path=row.full_path,
                is_terminal=row.is_terminal,
                tool_call_index=call_index,
            )
            for row in rows
        ]
    )


def _record_tree(ledger: TurnLedger, tree: CategoryTree, call_index: int) -> None:
    """Record the root and every node of the payload.

    `iter_tree_paths` rebuilds each node's `full_path` code-side with the same concatenation
    ingest used, so the strings match the stored ones exactly — which matters because
    `emit_classification` renders from this and nothing may ever read a bare description
    (2,473 of the 10,490 leaves are literally "інші").
    """
    evidence = [
        CodeEvidence(
            code=tree.code,
            description=tree.description,
            full_path=tree.full_path,
            is_terminal=False,
            tool_call_index=call_index,
        )
    ]
    evidence += [
        CodeEvidence(
            code=node.code,
            description=node.description,
            full_path=full_path,
            is_terminal=node.is_terminal,
            tool_call_index=call_index,
        )
        for node, full_path in iter_tree_paths(tree)
    ]
    ledger.record_codes(evidence)


async def _breadcrumb(repo: TariffRepo, code: str) -> list[PathStep]:
    """Ancestors, section first.

    Never raises: a missing breadcrumb must not take down the result that carries it, and
    v1's one dropped breadcrumb was on the error path.
    """
    try:
        rows = await repo.ancestry(code)
    except Exception:
        # Broad on purpose. If the database is genuinely down the tool's main query fails too
        # and the outer handler reports it; losing a decoration must not lose the result.
        return []
    return [PathStep(level=r.level, code=r.code, description=r.description) for r in rows]


async def _task(ctx: Ctx, title: str, content: str, icon: str) -> None:
    """One step of the live drill-down.

    Content is unique by construction — it always carries the code and a count — because
    `add_workflow_task` computes the streamed index with `list.index()`, pydantic models
    compare by value, and two identical tasks would make the client update task 0 instead of
    appending a second one.
    """
    await ctx.context.add_workflow_task(
        CustomTask(title=title, content=content, icon=icon, status_indicator="complete")  # type: ignore[arg-type]
    )


def _trail(path: list[PathStep]) -> str:
    return " › ".join(step.description for step in path if step.description)


# ---------------------------------------------------------------------------
# The tools
# ---------------------------------------------------------------------------


@function_tool(output_type=NodeListResult)
async def list_groups_in_section(ctx: Ctx, section_code: str) -> NodeListResult:
    """Повертає групи (2 цифри) всередині одного розділу УКТЗЕД.

    Args:
        section_code: Код РОЗДІЛУ, дві цифри від "01" до "21". Це номер розділу, а не групи.
    """
    code = normalize_code(section_code)
    ledger, rec, repo = _begin(ctx, "list_groups_in_section", {"section_code": code})
    try:
        if not _SECTION_RE.match(code):
            ledger.end_call(rec, error="bad_section_code")
            return NodeListResult(
                level="section",
                path=[],
                nodes=[],
                error=f"Розділу '{section_code}' не існує: розділи мають коди від 01 до 21.",
                explanation=_EXPLAIN_RETRY,
            )
        rows = await repo.list_groups_in_section(code)
        path = await _breadcrumb(repo, code)
        _record_summaries(ledger, rows, rec.index)
        await _task(
            ctx, f"Розділ {code}", f"{_trail(path) or code} — {len(rows)} груп", "book-open"
        )
        ledger.end_call(rec, digest=f"groups:{len(rows)}")
        return NodeListResult(
            level="section",
            path=path,
            nodes=rows,
            error=None if rows else f"У розділі {code} немає груп.",
            explanation=_EXPLAIN_GROUPS if rows else _EXPLAIN_DEAD_END,
        )
    except Exception as exc:
        ledger.end_call(rec, error=repr(exc))
        return NodeListResult(
            level="section", path=[], nodes=[],
            error="Довідник недоступний.", explanation=_EXPLAIN_UNAVAILABLE,
        )


@function_tool(output_type=NodeListResult)
async def list_categories_in_group(ctx: Ctx, group_code: str) -> NodeListResult:
    """Повертає товарні позиції (4 цифри) всередині однієї групи УКТЗЕД.

    Args:
        group_code: Код ГРУПИ, дві цифри від "01" до "97". Це номер групи, а не розділу.
    """
    code = normalize_code(group_code)
    ledger, rec, repo = _begin(ctx, "list_categories_in_group", {"group_code": code})
    try:
        if not _GROUP_RE.match(code):
            ledger.end_call(rec, error="bad_group_code")
            return NodeListResult(
                level="group",
                path=[],
                nodes=[],
                error=f"Код групи має складатися рівно з двох цифр. Отримано: '{group_code}'.",
                explanation=_EXPLAIN_RETRY,
            )
        rows = await repo.list_categories_in_group(code)
        path = await _breadcrumb(repo, code)
        _record_summaries(ledger, rows, rec.index)
        await _task(ctx, f"Група {code}", f"{_trail(path) or code} — {len(rows)} позицій", "search")
        ledger.end_call(rec, digest=f"categories:{len(rows)}")
        # An empty list means "unknown group" OR "reserved group" (15/77). The repo cannot
        # tell them apart cheaply and an empty list on its own invites a retry loop, so say so.
        return NodeListResult(
            level="group",
            path=path,
            nodes=rows,
            error=None if rows else f"У групі {code} немає позицій.",
            explanation=_EXPLAIN_CATEGORIES if rows else _EXPLAIN_EMPTY_GROUP,
        )
    except Exception as exc:
        ledger.end_call(rec, error=repr(exc))
        return NodeListResult(
            level="group", path=[], nodes=[],
            error="Довідник недоступний.", explanation=_EXPLAIN_UNAVAILABLE,
        )


async def _open(ctx: Ctx, tool_name: str, arg_name: str, raw: str) -> CategoryResult:
    """Shared body of `open_category` and `expand`: one adaptive subtree read."""
    code = normalize_code(raw)
    ledger, rec, repo = _begin(ctx, tool_name, {arg_name: code})
    is_open = tool_name == "open_category"
    pattern = _CATEGORY_RE if is_open else _PREFIX_RE
    expected = 'чотири цифри, наприклад "8517"' if is_open else "4, 6 або 8 цифр"
    try:
        if not pattern.match(code):
            ledger.end_call(rec, error="bad_code")
            return CategoryResult(
                tree=None,
                error=f"Код '{raw}' не підходить для {tool_name}: потрібно {expected}.",
                explanation=_EXPLAIN_RETRY,
            )
        tree = await (repo.open_category(code) if is_open else repo.expand(code))
    except UnknownCodeError:
        ledger.end_call(rec, error="not_found")
        trail = _trail(await _breadcrumb(repo, code[:2]))
        return CategoryResult(
            tree=None,
            error=f"Коду '{code}' немає в довіднику.",
            explanation=(
                (f"Ти зараз у гілці: {trail}. " if trail else "")
                + f'Перевір код: доступні позиції групи можна отримати через '
                f'list_categories_in_group("{code[:2]}").'
            ),
        )
    except Exception as exc:
        ledger.end_call(rec, error=repr(exc))
        return CategoryResult(
            tree=None, error="Довідник недоступний.", explanation=_EXPLAIN_UNAVAILABLE
        )

    _record_tree(ledger, tree, rec.index)
    if tree.is_dead_end:
        explanation = _EXPLAIN_DEAD_END
    elif tree.truncated:
        explanation = f"{_EXPLAIN_TERMINAL} {_EXPLAIN_TRUNCATED}"
    else:
        explanation = _EXPLAIN_TERMINAL
    await _task(
        ctx,
        f"{'Позиція' if is_open else 'Підпозиція'} {code}",
        f"{tree.description} — {tree.terminal_count} кінцевих кодів",
        "cube",
    )
    ledger.end_call(rec, digest=f"tree:{tree.terminal_count}:truncated={tree.truncated}")
    return CategoryResult(tree=tree, error=None, explanation=explanation)


@function_tool(output_type=CategoryResult)
async def open_category(ctx: Ctx, category_code: str) -> CategoryResult:
    """Розкриває товарну позицію: повертає її коди з позначкою "термінальний".

    Коди з "термінальний": true мають рівно 10 цифр — саме їх використовують для
    класифікації. Коди з "термінальний": false проміжні (6 або 8 цифр) і мають
    підпозиції. Якщо гілка велика, у відповіді буде "згорнуто": true — тоді відкрий
    її через expand.

    Args:
        category_code: Код позиції, чотири цифри, наприклад "8517".
    """
    return await _open(ctx, "open_category", "category_code", category_code)


@function_tool(output_type=CategoryResult)
async def expand(ctx: Ctx, prefix: str) -> CategoryResult:
    """Повертає підпозиції проміжного коду.

    Args:
        prefix: Код на 4, 6 або 8 цифр. У 10-значних кодів підпозицій немає — це листя.
    """
    return await _open(ctx, "expand", "prefix", prefix)


@function_tool(output_type=SearchResult)
async def search_candidates(ctx: Ctx, query: str, limit: int) -> SearchResult:
    """Шукає в довіднику УКТЗЕД за описом товару і повертає гіпотези з повним шляхом.

    Це найшвидший спосіб знайти точку входу, коли неочевидно, з якого розділу починати.
    Результат — ГІПОТЕЗИ, а не відповідь: перш ніж видавати код, відкрий відповідну
    позицію через open_category і переконайся сам.

    Пиши запит як опис товару своїми словами, з матеріалом і призначенням
    ("акумуляторний дриль з літій-іонною батареєю, ручний електроінструмент"), а не як
    одну торгову назву.

    Args:
        query: Опис товару українською. Матеріал, призначення, ступінь обробки.
        limit: Скільки гіпотез повернути. Розумно від 5 до 20.
    """
    text = " ".join((query or "").split())
    capped = max(1, min(int(limit or 10), MAX_SEARCH_LIMIT))
    ledger, rec, repo = _begin(ctx, "search_candidates", {"query": text, "limit": capped})
    try:
        if not text:
            ledger.end_call(rec, error="empty_query")
            return SearchResult(
                search=CandidateList(query=text, candidates=[]),
                error="Порожній запит.",
                explanation=_EXPLAIN_RETRY,
            )
        rows = await repo.search_candidates(text, capped)
    except Exception as exc:
        ledger.end_call(rec, error=repr(exc))
        return SearchResult(
            search=CandidateList(query=text, candidates=[]),
            error="Пошук недоступний.",
            explanation="Пройди довідником вручну: list_categories_in_group і open_category.",
        )
    # Recorded like every other tool. `emit_classification` additionally requires that the
    # code's branch was OPENED this turn, so a search hit cannot become an answer on its own —
    # that is what stops the retrieval shortcut degrading into "echo the top hit".
    _record_summaries(ledger, rows, rec.index)
    await _task(ctx, "Пошук у довіднику", f"«{text[:60]}» — {len(rows)} гіпотез", "compass")
    ledger.end_call(rec, digest=f"candidates:{len(rows)}")
    return SearchResult(
        search=CandidateList(query=text, candidates=rows),
        error=None,
        explanation=_EXPLAIN_CANDIDATES if rows else _EXPLAIN_NO_CANDIDATES,
    )


@function_tool(output_type=ResolveResult)
async def resolve_code(ctx: Ctx, code: str) -> ResolveResult:
    """Знаходить конкретний код у довіднику і повертає його повний опис та шлях.

    Використовуй, коли код назвав користувач, або щоб перевірити, чи код кінцевий і що саме
    він означає. Двозначний код тут завжди означає ГРУПУ: розділи не мають власних кодів у
    цьому інструменті.

    Args:
        code: Код УКТЗЕД на 2, 4, 6, 8 або 10 цифр, без пробілів і крапок.
    """
    normalized = normalize_code(code)
    ledger, rec, repo = _begin(ctx, "resolve_code", {"code": normalized})
    try:
        if not _CODE_RE.match(normalized):
            ledger.end_call(rec, error="bad_code")
            return ResolveResult(
                node=None,
                path=[],
                error=f"'{code}' не схоже на код УКТЗЕД: потрібно 2, 4, 6, 8 або 10 цифр.",
                explanation=_EXPLAIN_RETRY,
            )
        detail = await repo.resolve(normalized)
    except Exception as exc:
        ledger.end_call(rec, error=repr(exc))
        return ResolveResult(
            node=None, path=[], error="Довідник недоступний.", explanation=_EXPLAIN_UNAVAILABLE
        )

    if detail is None:
        ledger.end_call(rec, error="not_found")
        return ResolveResult(
            node=None,
            path=[],
            error=f"Коду {normalized} немає в довіднику.",
            explanation=(
                "Не видавай цей код. Пройди до потрібної позиції через search_candidates "
                "або list_categories_in_group."
            ),
        )

    path = await _breadcrumb(repo, normalized)
    ledger.record_codes(
        [
            CodeEvidence(
                code=detail.code,
                description=detail.description,
                full_path=detail.full_path,
                is_terminal=detail.is_terminal,
                tool_call_index=rec.index,
            )
        ]
    )
    if detail.is_dead_end:
        explanation = _EXPLAIN_DEAD_END
    elif detail.path_is_ambiguous:
        explanation = _EXPLAIN_AMBIGUOUS
    elif detail.is_terminal:
        explanation = _EXPLAIN_TERMINAL
    else:
        explanation = (
            f"Код {normalized} проміжний, а не кінцевий: у нього {detail.child_count} "
            "нащадків. Відкрий його через expand і обери кінцевий код."
        )
    await _task(
        ctx,
        f"Код {normalized}",
        f"{detail.description} — "
        f"{'кінцевий' if detail.is_terminal else f'{detail.child_count} підпунктів'}",
        "document",
    )
    ledger.end_call(rec, digest=f"resolve:{detail.level}:terminal={detail.is_terminal}")
    return ResolveResult(node=detail, path=path, error=None, explanation=explanation)


NAVIGATION_TOOLS = [
    list_groups_in_section,
    list_categories_in_group,
    open_category,
    expand,
    search_candidates,
    resolve_code,
]
"""Order is for readability only; the model picks by name and docstring."""
