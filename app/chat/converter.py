"""Thread items -> model input.

One override, for one reason. The default `widget_to_input` dumps the ENTIRE widget
JSON into the next turn's input as a user message. A result card carries the full code path
and the full description, so leaving the default on means re-sending the whole answer on every
subsequent turn of the conversation — silent growth that inflates the input token bill and the
wall clock of every model call as the thread gets longer.

Everything else is inherited on purpose. `structured_input_to_input` already renders a
clarification answer as the `<StructuredInput>` block the prompt expects, and
`hidden_context_to_input` is unreachable because this app never persists a `HiddenContextItem`:
turn 1's candidates for product A must not be replayed on turn 5 about product B.
"""

from __future__ import annotations

from chatkit.agents import ThreadItemConverter
from chatkit.types import WidgetItem
from openai.types.responses import ResponseInputTextParam
from openai.types.responses.response_input_item_param import Message

__all__ = ["UktzedConverter", "converter"]


class UktzedConverter(ThreadItemConverter):
    """The project's converter. One override; see the module docstring."""

    async def widget_to_input(self, item: WidgetItem) -> Message:
        # `copy_text` is the widget's own short form, authored next to the widget. When it is
        # missing a generic line is still strictly better than the full JSON: the codes the
        # model may use are re-established by tool calls in the current turn anyway, which is
        # the whole point of the turn-scoped ledger.
        summary = (item.copy_text or "").strip() or "картка результату"
        return Message(
            type="message",
            role="user",
            content=[
                ResponseInputTextParam(
                    type="input_text",
                    text=f"Користувачу показано картку результату: {summary}",
                )
            ],
        )


converter = UktzedConverter()
"""Stateless, so one instance for the process."""
