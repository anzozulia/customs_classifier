# ruff: noqa: RUF002  -- Ukrainian text below. Single-letter Cyrillic words and
# the en dash are correct typography here, not Latin look-alikes; pyproject pins ruff's
# defaults (no allowed-confusables), so the exemption is declared per file.
"""The single classifier agent.

ONE agent. No sub-agents, no handoffs. v1's cold cost was 4.72 model turns against ~2 warm,
and every handoff is a turn — an invisible one, because `stream_agent_response` drops
`AgentUpdatedStreamEvent` entirely. Web research is the hosted `WebSearchTool`, which resolves
inside the same Responses call instead of costing a call, a nested run and a consume turn.

Run termination is a `ToolsToFinalOutputFunction`, NOT `StopAtTools`. Verified at
`agents/run_internal/turn_resolution.py:766-773` (tag v0.22.2): `stop_at_tool_names` matches on
`tool_result.tool.name` — the CALL, not the result. A rejected `emit_classification` would end
the run anyway and the prompt's "виправ і виклич знову" would be a lie the code could not keep.
The callable sees the `Ack` and can send the model back round.
"""

from __future__ import annotations

from functools import lru_cache

from agents import (
    Agent,
    FunctionToolResult,
    ModelSettings,
    RunContextWrapper,
    ToolsToFinalOutputResult,
    WebSearchTool,
    set_tracing_disabled,
)
from openai.types.shared import Reasoning

from app.agent.context import UktzedContext
from app.agent.schemas import Ack
from app.agent.tools_nav import (
    expand,
    list_categories_in_group,
    list_groups_in_section,
    open_category,
    resolve_code,
    search_candidates,
)
from app.agent.tools_terminal import (
    TERMINAL_TOOL_NAMES,
    ask_clarification,
    emit_classification,
    stream_assistant_message,
    summarise_rejection,
)
from app.settings import get_settings

__all__ = ["AGENT_NAME", "build_agent", "finalize_on_terminal_tool", "model_settings"]

AGENT_NAME = "uktzed-classifier"

# Before any Agent or Runner is constructed. v1 shipped 5,482 trace batches offsite with zero
# consumers and every customer's product description in them. `ModelSettings(store=False)`
# below is a separate switch: tracing controls the dashboard, `store` controls retention.
set_tracing_disabled(True)


def model_settings() -> ModelSettings:
    """Always passed explicitly.

    `Agent.__post_init__` re-derives settings from a regex on the model name, and every
    `^gpt-5.6-…` pattern maps to `effort="none"` — which contradicts the API's own documented
    default and which the web-search guide warns degrades search quality. An `Agent(model=…)`
    with no settings behaves very differently from a raw Responses call with the same model.
    """
    settings = get_settings()
    return ModelSettings(
        # summary="auto" is what makes ChatKit render the Thinking panel at all.
        reasoning=Reasoning(effort=settings.reasoning_effort, summary="auto"),  # type: ignore[arg-type]
        verbosity="low",
        timeout=90.0,  # per model-call attempt. v1 had none and ate 600s read timeouts twice.
        include_usage=True,  # v1 never recorded a single token count.
        store=False,
        parallel_tool_calls=True,
    )


async def finalize_on_terminal_tool(
    ctx: RunContextWrapper[UktzedContext],
    results: list[FunctionToolResult],
) -> ToolsToFinalOutputResult:
    """End the run when a terminal tool SUCCEEDS; send the model back round when it does not.

    `is_final_output=False` means, verbatim from `agents/agent.py:79-82`, "the LLM will run
    again and receive the tool call output" — so the rejection message in the `Ack` is exactly
    what the model reads next. Bounded by `settings.max_repairs`, because an unbounded repair
    loop is just `max_turns` with extra steps.
    """
    max_repairs = get_settings().max_repairs
    for result in results:
        if result.tool.name not in TERMINAL_TOOL_NAMES:
            continue
        # `FunctionToolResult.output` is the raw Python return value, so this is the Ack
        # itself and not its serialised form (tool_execution.py:2306-2312).
        ack = result.output
        if isinstance(ack, Ack) and ack.ok:
            return ToolsToFinalOutputResult(is_final_output=True, final_output=ack)

        ctx.context.repairs += 1
        if ctx.context.repairs > max_repairs:
            # Give up rather than loop — but never in silence. When the run ends here the
            # model emits no assistant message, so this is the last chance to put something
            # on the user's screen, and "every terminal tool is total" has to hold for the
            # give-up path too.
            ctx.context.outcome = "error"
            await stream_assistant_message(ctx.context, summarise_rejection(ack))
            return ToolsToFinalOutputResult(is_final_output=True, final_output=ack)
        return ToolsToFinalOutputResult(is_final_output=False, final_output=None)
    return ToolsToFinalOutputResult(is_final_output=False, final_output=None)


@lru_cache(maxsize=2)
def build_agent(instructions: str) -> Agent[UktzedContext]:
    """Build the agent for one rendered system prompt.

    Cached because the prompt is static per dataset and rebuilding nine tool definitions on every
    turn is pure waste. Keyed on the instructions so a prompt change is picked up for free.
    """
    settings = get_settings()
    return Agent[UktzedContext](
        name=AGENT_NAME,
        model=settings.model,
        model_settings=model_settings(),
        instructions=instructions,  # STATIC. Never built from user text.
        tools=[
            list_groups_in_section,
            list_categories_in_group,
            open_category,
            expand,
            search_candidates,
            resolve_code,
            WebSearchTool(
                user_location={"type": "approximate", "country": "UA"},
                search_context_size="low",
            ),
            emit_classification,
            ask_clarification,
        ],
        tool_use_behavior=finalize_on_terminal_tool,
    )
