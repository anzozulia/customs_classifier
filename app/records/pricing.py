"""What a turn cost, in dollars — from a table, never from a constant.

`cost_usd` on a history row is the kind of number people paste into a budget spreadsheet, so
two properties matter more than convenience:

**It is a versioned table keyed by model id.** A single `COST_PER_1K = 0.002` somewhere in the
codebase is wrong the moment a second model appears or a price changes, and it is wrong
*silently and retroactively* — every historical row recomputed at today's rate. Prices here are
per model id, `PRICING_VERSION` says which price list they are, and the value is written onto
the row at finish time, so a later price change never rewrites what an old turn actually cost.

**An unknown model costs NULL, not a guess.** `price_usd` returns `None` for any model id not
in the table below, including a dated snapshot (`gpt-5.6-terra-2026-01-15`) of a model that IS
in it — the snapshot may well be priced differently, and inheriting the base price would be a
guess wearing six decimal places. Add the snapshot to the table to price it. The SPA already
renders a missing cost as «—»; a plausible wrong number has nowhere to surface as wrong.

**Cached tokens are a SUBSET of input tokens, and this was checked, not assumed.**
`app/chat/server.py` records `usage.input_tokens` and `usage.input_tokens_details.cached_tokens`
as two separate numbers, and in `agents/usage.py` both come straight off the Responses API's
`response.usage`: `input_tokens` is the total and `input_tokens_details` is a *breakdown of it*
(`Usage.add()` sums the two independently, so an aggregate over several model calls preserves
the same relationship). So the bill is `(tokens_in - tokens_cached)` at the input rate plus
`tokens_cached` at the cached rate — NOT `tokens_in` at full rate plus a cached surcharge,
which on a long tariff-walk conversation with a warm prompt cache would overstate the cost by
roughly the cache hit rate. `_fresh_input_tokens` clamps rather than trusts, so a provider that
ever reported the two as siblings yields a too-high cost instead of a negative one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Final

logger = logging.getLogger("uktzed.records")

__all__ = ["MODEL_PRICES", "PRICING_VERSION", "ModelPrice", "price_usd"]

PRICING_VERSION: Final[str] = "2026-09-16"
"""The price list these numbers came from. Bump it in the same commit that edits a price."""

_PER_MTOK: Final = Decimal(1_000_000)
_MICRO_USD: Final = Decimal("0.000001")  # cost_usd is NUMERIC(10,6)


@dataclass(frozen=True, slots=True)
class ModelPrice:
    """USD per 1,000,000 tokens. Decimal everywhere — money is never a float."""

    input_per_mtok: Decimal
    cached_input_per_mtok: Decimal
    output_per_mtok: Decimal


MODEL_PRICES: Final[dict[str, ModelPrice]] = {
    # The default from app/settings.py.
    "gpt-5.6-terra": ModelPrice(
        input_per_mtok=Decimal("2.00"),
        cached_input_per_mtok=Decimal("0.20"),
        output_per_mtok=Decimal("12.00"),
    ),
    # The small sibling: a tenth of the price, same shape. Kept here because it is what an
    # eval sweep or a cheap re-run would be pointed at.
    "gpt-5.6-luna": ModelPrice(
        input_per_mtok=Decimal("0.20"),
        cached_input_per_mtok=Decimal("0.02"),
        output_per_mtok=Decimal("1.20"),
    ),
}


def _fresh_input_tokens(tokens_in: int, tokens_cached: int) -> int:
    """Input tokens billed at the full rate.

    Cached tokens are a subset of `tokens_in` (see the module docstring), so this is a
    subtraction. The clamp is defensive: `max(…, 0)` would silently absorb a provider that
    reported the two as siblings, so the anomaly is logged and the *whole* input is billed at
    the full rate — erring towards over-reporting a cost rather than under-reporting it.
    """
    if tokens_cached > tokens_in:
        logger.warning(
            "cached tokens (%s) exceed input tokens (%s); billing the full input at the "
            "uncached rate",
            tokens_cached,
            tokens_in,
        )
        return tokens_in
    return tokens_in - tokens_cached


def price_usd(
    model: str | None,
    tokens_in: int | None,
    tokens_cached: int | None,
    tokens_out: int | None,
) -> Decimal | None:
    """Cost of one turn, rounded to the micro-dollar, or `None` for an unpriced model.

    Token counts are `int | None` because a turn that dies before its first model call has no
    usage at all; those count as zero, which makes the cost a true `0.000000` rather than an
    unknown. Only an unknown *model* yields `None`.
    """
    price = MODEL_PRICES.get(model or "")
    if price is None:
        # Not `exception`/`error`: an unpriced model is a missing table entry, not a failure,
        # and it happens on every single turn until someone adds the row. One warning line
        # with the model id is what makes that discoverable without drowning the log.
        logger.warning(
            "no price for model %r (pricing list %s); cost_usd will be NULL", model, PRICING_VERSION
        )
        return None

    billed_in = _fresh_input_tokens(tokens_in or 0, tokens_cached or 0)
    total = (
        billed_in * price.input_per_mtok
        + (tokens_cached or 0) * price.cached_input_per_mtok
        + (tokens_out or 0) * price.output_per_mtok
    ) / _PER_MTOK
    return total.quantize(_MICRO_USD, rounding=ROUND_HALF_UP)
