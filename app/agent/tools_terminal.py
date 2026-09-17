
# the en dash are correct typography here, not Latin look-alikes; pyproject pins ruff's
# defaults (no allowed-confusables), so the exemption is declared per file.
"""Terminal tools — the two ways a turn can end with an artefact.

v1's three-way outcome contract survives in full (1,130 result / 244 clarification / 29
conversation of 1,403); only its carrier changes. `emit_classification` is the *result* branch,
`ask_clarification` is the *clarification* branch, and calling neither is the *conversation*
branch, which streams as prose for free.

Two rules govern everything here.

**The gate is code, not prose.** v1 shouted "не вигадуй і не галюцинуй коди" and then printed
whatever came back — no length check, no existence check, no description check, 100% prompt and
0% code. Here the checks run inside the tool, before anything renders:

| # | Check | Owner |
|---|---|---|
| E1 | exactly ten digits | this module |
| E2 | exists in the active dataset | `app.tariff.validate` |
| E3 | `is_terminal`, the authored column, never `len(code) == 10` | `app.tariff.validate` |
| E4 | this turn's tools surfaced it, and the branch was actually opened | `TurnLedger` + here |
| E5 | every alternative passes E1-E4 | here |
| E6 | at most four alternatives — truncate, do not reject | here |
| E7 | the model's `full_description` against the stored `full_path` | metric, not an error |

E4 is the one that matters. v1's measured failure was not cold hallucination (~1.3% of fresh
answers) but STALE CARRY-OVER: 36% of continued turns answered from tool output replayed in
context from an earlier turn, potentially about a different product. Scoping provenance to the
current turn kills both, and it is the reason a retrieval hit cannot become an answer by itself.

**Every terminal tool is total.** When the run ends on a tool result the model emits no
assistant message, so anything the tool does not stream is silence on the user's screen. Both
tools stream a visible item on every path they can end on, and `finalize_on_terminal_tool`
streams one on the give-up path.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any, Final, Literal

from agents import function_tool
from chatkit.actions import ActionConfig
from chatkit.types import (
    AssistantMessageContent,
    AssistantMessageItem,
    CustomSummary,
    StructuredInputFreeform,
    StructuredInputItem,
    StructuredInputMultipleChoice,
    StructuredInputMultipleChoiceOption,
    ThreadItemDoneEvent,
)
from chatkit.widgets import Button, Card
from pydantic import Field

from app.agent import PathStep, UAModel
from app.agent.context import Ctx, UktzedContext
from app.agent.provenance import CodeEvidence, ProvenanceError
from app.agent.schemas import Ack, Alternative, NodeDetail
from app.tariff import PATH_SEPARATOR
from app.tariff.repo import normalize_code
from app.tariff.validate import CodeStatus, message_for, validate_and_resolve

__all__ = [
    "TERMINAL_TOOL_NAMES",
    "ClassifiedCode",
    "ask_clarification",
    "emit_classification",
    "format_code",
    "stream_assistant_message",
    "summarise_rejection",
]

logger = logging.getLogger("uktzed.agent")

_TEN_DIGITS = re.compile(r"^\d{10}$")

MAX_PRIMARY_CODES = 3
"""2,487 terminal codes (23.71%) share a text path with a sibling, because the real tariff
separates them by header text this snapshot dropped. For those the honest answer is several
codes shown together — but three is already where a customs officer stops reading."""

MAX_ALTERNATIVES = 4  # E6
MAX_OPTIONS = 5  # A2

_NAVIGATION_TOOLS = {"open_category", "expand", "resolve_code"}
"""The only tools that put a TERMINAL code in front of the model by walking to it.

`search_candidates` is deliberately absent: its hits are trigram hypotheses over `full_path`,
and without this distinction pre-retrieval would collapse into echoing the top hit. Note that
`list_categories_in_group` is absent too — it returns 4-digit headings, never a terminal code,
so counting it as a walk would let a group listing launder a search hit.
"""

TERMINAL_TOOL_NAMES = {"emit_classification", "ask_clarification"}

# The closed rejection vocabulary. Every one is repairable by the model, which is the entire
# reason `tool_use_behavior` is a callable and not `StopAtTools`.
ERROR_CODE_FORMAT = "code_format"
ERROR_NOT_IN_DATASET = "code_not_in_dataset"
ERROR_NOT_LEAF = "code_not_leaf"
ERROR_PROVENANCE = "provenance_violation"
ERROR_NOT_NAVIGATED = "code_not_navigated"
ERROR_NO_CODES = "no_codes"
ERROR_EMPTY_QUESTION = "empty_question"


def format_code(code: str) -> str:
    """`8517130000` -> `8517 13 00 00`. The ONLY place a code is formatted.

    v1 delegated this to model obedience, in prose, in two files, plus a `00`-padding rule that
    was a band-aid over a data bug.
    """
    digits = normalize_code(code)
    if len(digits) != 10:
        return digits
    return f"{digits[0:4]} {digits[4:6]} {digits[6:8]} {digits[8:10]}"


class ClassifiedCode(UAModel):
    """One code the model is putting its name to.

    `full_description` is what the model *believes* the full path is. It is checked against the
    database (E7) and then discarded: the rendered answer always carries the stored `full_path`.
    """

    code: str = Field(alias="код")
    full_description: str = Field(alias="повний_опис")


class _Checked:
    """A code that passed E1-E4, carrying the database's own text."""

    __slots__ = ("code", "detail", "status")

    def __init__(self, code: str, detail: NodeDetail, status: CodeStatus) -> None:
        self.code = code
        self.detail = detail
        self.status = status


def _navigated_prefixes(ctx: Ctx) -> set[str]:
    """Codes this turn actually opened, read straight off the ledger's call log.

    No extra state to keep in sync: `ToolCallRecord` already stores the tool name and its
    arguments, which is exactly the question "did the model walk here, or did a search merely
    mention it?".
    """
    prefixes: set[str] = set()
    for call in ctx.context.ledger.calls:
        if call.tool_name not in _NAVIGATION_TOOLS:
            continue
        for key in ("category_code", "prefix", "code"):
            value = call.args.get(key)
            if value:
                prefixes.add(str(value))
    return prefixes


def _surfaced_by(ctx: Ctx, evidence: CodeEvidence) -> str:
    """Which tool first put this code in front of the model.

    `TurnLedger.record_codes` is first-sighting-wins, so a code that search found and the model
    later opened still points at the search call. That is why the walk test below is an OR: the
    origin may be a navigation tool, or a navigation call may cover the code by prefix.
    """
    calls = ctx.context.ledger.calls
    index = evidence.tool_call_index
    return calls[index].tool_name if 0 <= index < len(calls) else ""


async def _check_code(ctx: Ctx, raw: str, *, navigated: set[str]) -> _Checked | Ack:
    """E1-E4 for one code. Returns the database row on success, a repairable `Ack` on failure."""
    code = normalize_code(raw)

    if not _TEN_DIGITS.match(code):  # E1
        return Ack(
            ok=False,
            error_code=ERROR_CODE_FORMAT,
            message=(
                f"Код має містити рівно 10 цифр без пробілів. Отримано: '{raw}'. "
                "Виправ і виклич знову."
            ),
        )

    try:  # E4 — provenance
        evidence: CodeEvidence = ctx.context.ledger.require(code)
    except ProvenanceError:
        return Ack(
            ok=False,
            error_code=ERROR_PROVENANCE,
            message=(
                f"Ти не відкривав код {code} у довіднику в цьому запиті. Коди з попередніх "
                "повідомлень стосуються інших товарів. Пройди до нього через open_category "
                "і виклич знову."
            ),
        )

    # E2 + E3 — against the database, through the same function the offline validator uses, so
    # the wording and the condition cannot drift apart the way v1's did.
    repo = ctx.context.tariff
    if repo is None:
        # No repo wired (unit tests, the M0 spike): the ledger's evidence came from the
        # database this turn, so it is the same row, read once instead of twice.
        detail = NodeDetail(
            code=code,
            level="code",
            description=evidence.description,
            full_path=evidence.full_path,
            is_terminal=evidence.is_terminal,
            depth=0,
            child_count=0,
            ancestor_codes=[],
            path_is_ambiguous=False,
            is_dead_end=False,
        )
        status = CodeStatus.VALID if evidence.is_terminal else CodeStatus.NOT_TERMINAL
    else:
        status, detail = await validate_and_resolve(repo, code)
        if detail is None:
            return Ack(
                ok=False,
                error_code=ERROR_NOT_IN_DATASET,
                message=message_for(CodeStatus.NOT_FOUND, code),
            )

    if not status.is_pass:
        return Ack(
            ok=False,
            error_code=ERROR_NOT_LEAF,
            message=message_for(status, code, detail),
        )

    # E4, second half — the walk has to have happened. Not guarded on `navigated` being
    # non-empty: an empty set means nothing was opened at all, which is exactly the case this
    # is here to reject.
    walked = _surfaced_by(ctx, evidence) in _NAVIGATION_TOOLS or any(
        code.startswith(prefix) for prefix in navigated
    )
    if not walked:
        return Ack(
            ok=False,
            error_code=ERROR_NOT_NAVIGATED,
            message=(
                f"Код {code} ти бачив лише серед гіпотез пошуку — це не відповідь. "
                f"Відкрий позицію {code[:4]} через open_category, переконайся сам "
                "і виклич знову."
            ),
        )

    return _Checked(code=code, detail=detail, status=status)


def _tail(full_path: str, steps: int = 2) -> str:
    """The last couple of segments of a stored `full_path`.

    The full path is what the user shows customs, so the emitted code keeps all of it. An
    alternative only has to be recognisable, and 46% of leaf descriptions are 15 characters or
    shorter (2,473 are literally "інші"), so one segment alone would not identify it either.
    """
    return PATH_SEPARATOR.join(full_path.split(PATH_SEPARATOR)[-steps:])


# Residual leaves. 2,473 of 10,490 leaf descriptions are literally "інші", so the deepest
# segment of a full_path is very often the least informative thing in it.
_RESIDUAL: Final[frozenset[str]] = frozenset({"інші", "інша", "інше", "інший", "решта"})
_HEADLINE_CHARS: Final[int] = 80


def _describe(full_path: str) -> tuple[str, str]:
    """(what this code specifically is, the heading it sits in).

    Three rounds of browser feedback produced this shape.

    A stored full_path is `section > chapter > heading > … > leaf`. Section and chapter are
    filing structure — section 16 is 200 characters of «Машини, обладнання та механізми; …»
    that describes no goods — so the description begins at the 4-digit HEADING.

    But leading with the heading is also wrong. Heading 8516 is a 400-character enumeration of
    water heaters, hair dryers, curling tongs and irons, and a coffee machine sits under one
    clause of it; heading 6109 is t-shirts and vests. What identifies the code is the NARROWING
    below the heading — «для приготування кави або чаю», «з бавовни».

    And the narrowing alone is not enough either: «з бавовни» names no goods, and 2,473 of
    10,490 leaves are literally «інші». So both are returned, and the caller shows the
    narrowing first and the heading as labelled context beneath it — the reader sees the
    specific answer and can still see what position it sits in.

    When every narrowing is residual there is nothing specific to lead with, so the heading
    becomes the description and the context line is empty.
    """
    segments = [seg.strip() for seg in full_path.split(PATH_SEPARATOR) if seg.strip()]
    if not segments:
        return "", ""
    below_section = segments[2:] if len(segments) > 2 else segments
    if not below_section:
        return segments[-1], ""

    heading, *narrowings = below_section
    if not narrowings:
        # The code hangs straight off its heading; there is no narrowing to lead with.
        return heading, ""

    # Every narrowing, residual ones included. «Інші → інші» reads oddly as a lead, but it is
    # what the code IS, and the context line directly beneath says which position it narrows.
    # The alternative — promoting the heading when the narrowings are uninformative — bolded
    # 240 characters of enumeration for heading 8525 and buried the answer again.
    # Case is left exactly as stored. The database already holds narrowings as the
    # mid-sentence fragments they are («з бавовни», «для приготування кави або чаю») and
    # headings capitalised, which is correct for both roles here. Re-casing them in one place
    # and un-casing them in another turned a whole heading into «машини, обладнання…».
    return " → ".join(narrowings), heading


def _breadcrumb(code: str) -> str:
    """Chapter > heading > code, DERIVED FROM THE CODE — not from the model's path.

    A 10-digit code carries its own ancestry: the first two digits are the chapter and the
    first four are the heading, by construction. Deriving the trail instead of reading the
    model's `path` means no label can be wrong and the section — which is not part of a code,
    and is not addressable by the tools — cannot leak in.

    Numbers only; the descriptions are already above, and repeating them was the redundancy
    that made the old answer unreadable.
    """
    digits = normalize_code(code)
    if len(digits) != 10:
        return f"**{format_code(code)}**"
    return " \u203a ".join([digits[:2], digits[:4], f"**{format_code(digits)}**"])


def _answer_markdown(
    checked: list[_Checked],
    confidence: str,
    rationale: str,
    product_summary: str,
    path: list[PathStep],
    alternatives: list[Alternative],
) -> str:
    """The answer, rendered from database text plus the model's reasoning.

    Markdown, not a `.widget` template: the widget is a later milestone, and a terminal tool
    that renders nothing at all is strictly worse than one that renders plainly.
    """
    lines: list[str] = []
    for index, item in enumerate(checked):
        if index:
            lines.append("---")
            lines.append("")
        # Number first and alone, because it is the thing being copied into a declaration.
        # Code and narrowing on ONE heading line. Split across two lines the narrowing was a
        # bold BODY fragment — visually smaller than the code above it and orphaned from the
        # heading below, reading as a caption that lost its picture. «6109 10 00 00 — з
        # бавовни» is a single answer and is set as one.
        specific, heading = _describe(item.detail.full_path)
        title = format_code(item.code)
        if specific:
            title = f"{title} — {specific}"
        lines += [f"### {title}", ""]
        # The position, in ordinary body text: context, and clearly secondary to the line above.
        if heading:
            lines += [heading, ""]
        if item.status is CodeStatus.AMBIGUOUS:
            lines += [f"_{message_for(CodeStatus.AMBIGUOUS, item.code, item.detail)}_", ""]

    if product_summary.strip():
        lines += [product_summary.strip(), ""]
    if rationale.strip():
        lines += [rationale.strip(), ""]

    if alternatives:
        lines += ["**Також розглянуто**", ""]
        lines += [
            f"- `{format_code(a.code)}` {_tail(a.description, 1)} — {a.reason}"
            for a in alternatives
        ]
        lines.append("")

    trail = _breadcrumb(checked[0].code) if checked else ""
    footer = trail or ""
    if footer:
        footer += " · "
    lines.append(f"{footer}впевненість: {confidence}")
    return "\n".join(lines).strip()


async def stream_assistant_message(agent_ctx: UktzedContext, text: str) -> str:
    """Stream one finished assistant message and return its id.

    `ThreadItemDoneEvent` is what persists it — never call `store.add_thread_item` here, the
    SDK already does that on Done and a second write is a primary-key violation.

    Takes the `AgentContext` rather than the `RunContextWrapper` so `finalize_on_terminal_tool`,
    which is not a tool, can render the give-up path too.
    """
    item_id = agent_ctx.generate_id("message")
    await agent_ctx.stream(
        ThreadItemDoneEvent(
            item=AssistantMessageItem(
                id=item_id,
                thread_id=agent_ctx.thread.id,
                created_at=datetime.now(),
                content=[AssistantMessageContent(text=text)],
            )
        )
    )
    return item_id


@function_tool(output_type=Ack)
async def emit_classification(
    ctx: Ctx,
    codes: list[ClassifiedCode],
    confidence: Literal["висока", "середня", "низька"],
    rationale: str,
    path: list[PathStep],
    alternatives: list[Alternative],
    product_summary: str,
    evidence: Literal["опис користувача", "веб-джерело", "відповідь на уточнення"],
) -> Ack:
    """Видає остаточний код УКТЗЕД користувачу. Завершує хід.

    Викликай рівно один раз і лише тоді, коли ти відкрив цей код у довіднику і перевірив
    його опис. Якщо інструмент поверне помилку — виправ і виклич знову; хід на цьому не
    закінчується.

    Args:
        codes: Кінцеві коди УКТЗЕД, які ти видаєш. Майже завжди один. Кілька — лише коли
            довідник не дозволяє розрізнити їх за описом. Кожен код рівно 10 цифр без
            пробілів і крапок, і кожен має бути кодом, який ти бачив у результаті
            open_category або expand у ЦЬОМУ запиті.
        confidence: "висока" — усі ознаки товару однозначно вкладаються в цей код;
            "середня" — код найкращий з розглянутих, але одна ознака лишилась неявною;
            "низька" — вибір неоднозначний, і ти зобов'язаний заповнити alternatives.
        rationale: 1–2 речення: ЧОМУ саме цей код і яка ознака відрізняє його від
            найближчого сусіда. НЕ переказуй тут товар — він уже надрукований рядком вище
            з product_summary, і повторення робить відповідь удвічі довшою без нової
            інформації. Починай одразу з ознаки: «Ширина 15 см не перевищує 20 см, тому…».
            Не цитуй назву позиції і не повторюй повний опис — його видно нижче.
        path: Пройдений шлях від розділу до коду, по одному кроку на рівень. Саме те, що
            ти відкривав інструментами, без домислів.
        alternatives: Коди, які ти серйозно розглядав і відхилив, з причиною відхилення.
            Порожній список, якщо альтернатив не було. Максимум 4. Обов'язковий, коли
            confidence не "висока".
        product_summary: ОДНЕ коротке речення: що саме за товар ти класифікував, як ти його
            зрозумів. Користувач за цим перевірить, чи правильно ти його зрозумів. Без
            маркетингових деталей і без переліку характеристик, які не вплинули на вибір.
        evidence: Звідки взято характеристики товару.
    """
    ledger = ctx.context.ledger
    rec = ledger.begin_call(
        "emit_classification",
        {"codes": [c.code for c in codes], "confidence": confidence, "evidence": evidence},
    )

    if not codes:
        ledger.end_call(rec, error=ERROR_NO_CODES)
        return Ack(
            ok=False,
            error_code=ERROR_NO_CODES,
            message="Список кодів порожній. Або виклич ask_clarification, або видай код.",
        )

    navigated = _navigated_prefixes(ctx)

    checked: list[_Checked] = []
    for item in codes[:MAX_PRIMARY_CODES]:
        outcome = await _check_code(ctx, item.code, navigated=navigated)
        if isinstance(outcome, Ack):
            ledger.end_call(rec, error=outcome.error_code)
            return outcome
        checked.append(outcome)

    # E5 + E6 — alternatives go through the same gate; the fifth and beyond are dropped, not
    # rejected, because "you listed too many" is not worth a model turn.
    clean_alternatives: list[Alternative] = []
    for alt in alternatives[:MAX_ALTERNATIVES]:
        outcome = await _check_code(ctx, alt.code, navigated=navigated)
        if isinstance(outcome, Ack):
            ledger.end_call(rec, error=outcome.error_code)
            return outcome
        # D25: description from the database, never from the model.
        clean_alternatives.append(
            Alternative(
                code=outcome.code, description=outcome.detail.full_path, reason=alt.reason
            )
        )

    # E7 — a metric, not an error, precisely because the answer never uses the model's text.
    mismatches = sum(
        1
        for item, source in zip(checked, codes, strict=False)
        if source.full_description.strip() != item.detail.full_path.strip()
    )

    await ctx.context.end_workflow(
        CustomSummary(title="Класифікацію завершено", icon="check-circle")
    )
    await stream_assistant_message(
        ctx.context,
        _answer_markdown(checked, confidence, rationale, product_summary, path, clean_alternatives),
    )
    await _stream_new_classification_button(ctx.context)

    ctx.context.outcome = "result"
    ctx.context.emitted_codes = [item.code for item in checked]
    ledger.end_call(rec, digest=f"ok:{len(checked)}:mismatch={mismatches}")
    return Ack(ok=True, error_code=None, message="Класифікацію показано користувачу.")


@function_tool(output_type=Ack)
async def ask_clarification(ctx: Ctx, question: str, options: list[str], why: str) -> Ack:
    """Ставить користувачу одне уточнююче питання. Завершує хід.

    Викликай, коли опис товару неточний, занадто загальний або неоднозначний, і жоден
    кінцевий код не можна обрати без додаткової ознаки.

    Args:
        question: Одне питання українською, сформульоване мовою користувача, а не мовою
            довідника.
        options: Від 2 до 5 конкретних варіантів відповіді, якщо їх можна назвати —
            користувач обере в один клік. Порожній список, якщо питання відкрите.
        why: Одне речення: на яких кодах ти зупинився і що саме ця ознака вирішує.
            Показується користувачу під питанням.
    """
    ledger = ctx.context.ledger
    rec = ledger.begin_call("ask_clarification", {"options": len(options or [])})

    text = (question or "").strip()
    if not text:  # A1
        # Total even here: the run can end on this result, so something must reach the screen.
        await stream_assistant_message(
            ctx.context,
            "Потрібно трохи більше інформації про товар — опишіть, будь ласка, з чого він "
            "зроблений і для чого призначений.",
        )
        ledger.end_call(rec, error=ERROR_EMPTY_QUESTION)
        return Ack(
            ok=False,
            error_code=ERROR_EMPTY_QUESTION,
            message="Питання порожнє. Сформулюй одне конкретне питання і виклич знову.",
        )

    # A2 + A3 — deduplicate, drop blanks, cap at five. One option is not a choice, so it
    # degrades to a freeform question rather than being rejected back to the model.
    seen: set[str] = set()
    clean: list[str] = []
    for option in options or []:
        value = (option or "").strip()
        if value and value not in seen:
            seen.add(value)
            clean.append(value)
        if len(clean) == MAX_OPTIONS:
            break
    if len(clean) < 2:
        clean = []

    hint = (why or "").strip()
    await ctx.context.end_workflow(
        CustomSummary(title="Потрібне уточнення", icon="circle-question")
    )
    if clean and hint:
        # A multiple-choice input has nowhere to put the reason, so it goes in the thread.
        await stream_assistant_message(ctx.context, hint)

    # The first-class primitive, not free text the user has to read and retype: ChatKit takes
    # over the composer, records the answer on the item, and feeds it back as a
    # <StructuredInput> block. One model turn instead of two, on 17.4% of v1's traffic.
    structured = (
        StructuredInputMultipleChoice(
            id=ctx.context.generate_id("message"),
            question=text,
            options=[StructuredInputMultipleChoiceOption(value=value) for value in clean],
            multiple=False,
        )
        if clean
        else StructuredInputFreeform(
            id=ctx.context.generate_id("message"), question=text, description=hint or None
        )
    )
    await ctx.context.stream(
        ThreadItemDoneEvent(
            item=StructuredInputItem(
                id=ctx.context.generate_id("message"),
                thread_id=ctx.context.thread.id,
                created_at=datetime.now(),
                status="pending",
                inputs=[structured],
            )
        )
    )

    ctx.context.outcome = "clarification"
    ledger.end_call(rec, digest=f"clarification:{len(clean)}")
    return Ack(ok=True, error_code=None, message="Питання показано користувачу.")


def summarise_rejection(ack: Any) -> str:
    """Honest, user-facing text for a terminal tool that never succeeded.

    Reached only from `finalize_on_terminal_tool` once the repair budget is spent — the one
    path where nothing else can put anything on the screen.
    """
    detail = getattr(ack, "message", None) or ""
    return (
        "Не вдалося підтвердити код у довіднику. Уточніть, будь ласка, опис товару — "
        "матеріал, призначення, ступінь обробки."
        + (f"\n\n_Технічна причина:_ {detail}" if detail else "")
    )

# The "one more product" affordance. A classification is a finished unit of work, and the
# next product is a NEW conversation, not a follow-up turn — v1's users pressed /clear 449
# times asking for exactly this boundary, 87% of them right after a delivered result.
#
# handler="client" means the action never reaches the server: the browser calls
# control.setThreadId(null) and a fresh thread starts. Nothing to persist, nothing to route.
#
# NOTE: constructing named widget classes directly is deprecated in openai-chatkit 1.6.5 in
# favour of `.widget` template files. We pin 1.6.5 exactly, and a single button does not earn
# a template file plus its JSON schema. Migrating is the follow-up if the pin ever moves.
NEW_CLASSIFICATION_ACTION = "new_classification"


async def _stream_new_classification_button(agent_ctx: UktzedContext) -> None:
    """Offer a clean thread for the next product, directly under the answer."""
    try:
        await agent_ctx.stream_widget(
            Card(
                size="full",
                padding=2,
                children=[
                    Button(
                        label="Класифікувати наступний товар",
                        style="secondary",
                        iconStart="plus",
                        onClickAction=ActionConfig(
                            type=NEW_CLASSIFICATION_ACTION,
                            handler="client",
                            # Nothing is streamed in response, so do not leave the widget
                            # sitting in a loading state waiting for a server turn.
                            loadingBehavior="none",
                            streaming=False,
                        ),
                    )
                ],
            )
        )
    except Exception:
        logger.exception("could not stream the new-classification button")

