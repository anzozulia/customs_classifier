"""The classification record: what was asked, what was answered, and what it cost.

Two modules, one table, and a deliberately small surface:

* `writer` — the three-call lifecycle (`begin_turn` / `finish_turn` / `fail_turn`) that
  `app/chat/server.py` drives. The row is INSERTed `pending` BEFORE the model runs, which is
  the entire reason this package exists: v1 wrote a record only on success and every crashed
  turn vanished.
* `pricing` — `price_usd()`, a versioned per-model price table.

Re-exported here so call sites read `from app.records import begin_turn` rather than reaching
two levels in. `routes` is NOT imported from this `__init__`: it is a FastAPI router that
pulls in the whole web stack, and the writer is used from the chat server and the CLI, which
must not pay for that import.
"""

from __future__ import annotations

from app.records.pricing import MODEL_PRICES, PRICING_VERSION, ModelPrice, price_usd
from app.records.writer import (
    DB_OUTCOME,
    RecordedCode,
    begin_turn,
    codes_from_ledger,
    fail_turn,
    finish_turn,
    new_classification_id,
)

__all__ = [
    "DB_OUTCOME",
    "MODEL_PRICES",
    "PRICING_VERSION",
    "ModelPrice",
    "RecordedCode",
    "begin_turn",
    "codes_from_ledger",
    "fail_turn",
    "finish_turn",
    "new_classification_id",
    "price_usd",
]
