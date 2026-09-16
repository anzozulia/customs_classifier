"""PgStore — the multi-user boundary.

`ChatKitServer` performs ZERO authorization. `_process_streaming_impl` passes a
client-supplied thread_id straight into `store.load_thread`, and `threads.update` is
rename-only and equally unguarded. So every one of the 14 methods below filters on
`context.user_id`, and "not yours" and "does not exist" produce the same `NotFoundError`
(→ 404). A 403 would confirm the id exists and turn an authenticated session into an
existence oracle.

Modelled on chatkit-python's own `tests/helpers/mock_store.py` — the only upstream
reference that enforces a user id at all — with its defects fixed:
  * `save_item` is a real upsert (a bare UPDATE silently no-ops for an id that is not yet
    stored, and this method is reached via ThreadItemReplacedEvent);
  * `load_attachment` is scoped;
  * ordering is by a database-assigned `seq` rather than `created_at`, which ChatKit fills
    with `datetime.now()` and which therefore ties.

Parameter names and positional order are part of the contract: the SDK calls
`load_thread_items` positionally in four places (including `stream_agent_response`'s
`(thread.id, None, 2, "desc", ctx)` on every turn) and by keyword elsewhere.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any, Final

import asyncpg
from chatkit.store import NotFoundError, Store, StoreItemType
from chatkit.types import Attachment, Page, ThreadItem, ThreadMetadata
from pydantic import TypeAdapter

from app.context import RequestContext

# ThreadItem and Attachment are Annotated discriminated unions, not classes: there is no
# `ThreadItemAdapter` exported from chatkit.types, so build the adapters here.
_ITEM: Final = TypeAdapter(ThreadItem)
_ATTACHMENT: Final = TypeAdapter(Attachment)

# Mirrors chatkit.store's private _ID_PREFIXES. Purely cosmetic — nothing parses these —
# but a `wf_…` id in a log is worth the four lines.
_ID_PREFIXES: Final[dict[str, str]] = {
    "thread": "thr",
    "message": "msg",
    "tool_call": "tc",
    "task": "tsk",
    "workflow": "wf",
    "attachment": "atc",
    "sdk_hidden_context": "shcx",
}


def _dump(model: Any) -> str:
    """Serialize a pydantic model for a JSONB column.

    mode="json" so datetimes become strings. Do NOT pass
    context={"exclude_metadata": True} — that is for the WIRE, not for storage; it would
    drop integration metadata we want to keep.
    """
    return json.dumps(model.model_dump(mode="json"))


def _aware(value: datetime) -> datetime:
    """ChatKit stamps items with `datetime.now()`, i.e. naive LOCAL time. Attach the local
    offset rather than declaring it UTC, which would shift every timestamp by the offset."""
    return value if value.tzinfo is not None else value.astimezone()


def _as_metadata(thread: ThreadMetadata) -> ThreadMetadata:
    """Normalise a `Thread` (ThreadMetadata + `items`) back down to `ThreadMetadata`.

    On `threads.create` the SDK builds a full `Thread` and `_process_events`' auto-save hands
    it here UNNORMALISED, on every mutation of the thread for the rest of the turn. Stored as
    given, each of those saves would write the whole in-memory items page into the thread row.

    The related famous failure does NOT reach a JSON-backed store: an in-memory store returns
    a `Thread` from load_thread, and `_load_full_thread`'s `Thread(**meta.model_dump(),
    items=…)` then raises "got multiple values for keyword argument 'items'" (both official
    in-memory stores have this bug), whereas `load_thread` here rehydrates through
    `ThreadMetadata.model_validate_json`, which drops the extra key. So this is about what we
    persist, not about what we return — and the test pins both halves.
    """
    if type(thread) is ThreadMetadata:
        return thread
    return ThreadMetadata.model_validate(thread.model_dump(exclude={"items"}))


class PgStore(Store[RequestContext]):
    """Every query filters on context.user_id. This class is the only thing between user A
    and user B."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    # ---- ids ---------------------------------------------------------------
    # NOT chatkit's default_generate_id: that is uuid4().hex[:8] — 32 bits, and thread ids
    # travel in URLs and in client requests. 24 bytes of token_urlsafe is 192 bits and the
    # same 32 characters on the wire.
    def generate_thread_id(self, context: RequestContext) -> str:
        return f"thr_{secrets.token_urlsafe(24)}"

    def generate_item_id(
        self, item_type: StoreItemType, thread: ThreadMetadata, context: RequestContext
    ) -> str:
        # NB there is no "structured_input" StoreItemType — the SDK mints StructuredInputItem
        # ids as "message".
        return f"{_ID_PREFIXES.get(item_type, 'itm')}_{secrets.token_urlsafe(18)}"

    # ---- the one ownership check -------------------------------------------
    async def _owned_thread(self, thread_id: str, context: RequestContext) -> str:
        """Return the thread's stored payload, or raise NotFoundError.

        One helper answers "is this thread this user's?" for every read path. The two write
        paths (add_thread_item / save_item) do NOT call it: they fold the same predicate
        into their own INSERT … SELECT, which proves ownership in the same statement and so
        has no TOCTOU window at all.
        """
        payload = await self.pool.fetchval(
            "SELECT payload FROM chat_thread WHERE id = $1 AND user_id = $2",
            thread_id,
            context.user_id,
        )
        if payload is None:
            raise NotFoundError(f"Thread {thread_id} not found")
        return payload

    # ---- threads -----------------------------------------------------------
    async def load_thread(self, thread_id: str, context: RequestContext) -> ThreadMetadata:
        return ThreadMetadata.model_validate_json(await self._owned_thread(thread_id, context))

    async def save_thread(self, thread: ThreadMetadata, context: RequestContext) -> None:
        thread = _as_metadata(thread)
        data = thread.model_dump(mode="json")
        # The WHERE on DO UPDATE is load-bearing: threads.update is rename-only and
        # unguarded, so without it user A can retitle user B's thread. With it, the
        # conflicting row simply is not updated and RETURNING yields nothing → 404.
        got = await self.pool.fetchval(
            """
            INSERT INTO chat_thread (id, user_id, title, metadata, payload, created_at, updated_at)
            VALUES ($1, $2, $3, $4::jsonb, $5::jsonb, $6, now())
            ON CONFLICT (id) DO UPDATE
               SET title = EXCLUDED.title,
                   metadata = EXCLUDED.metadata,
                   payload = EXCLUDED.payload,
                   updated_at = now()
             WHERE chat_thread.user_id = $2
            RETURNING id
            """,
            thread.id,
            context.user_id,
            thread.title,
            json.dumps(data["metadata"]),
            json.dumps(data),
            _aware(thread.created_at),
        )
        if got is None:
            raise NotFoundError(f"Thread {thread.id} not found")

    async def load_threads(
        self, limit: int, after: str | None, order: str, context: RequestContext
    ) -> Page[ThreadMetadata]:
        cmp_op, direction = (">", "ASC") if order == "asc" else ("<", "DESC")
        if after is None:
            rows = await self.pool.fetch(
                f"SELECT id, payload FROM chat_thread WHERE user_id = $1 "
                f"ORDER BY created_at {direction}, id {direction} LIMIT $2",
                context.user_id,
                limit + 1,
            )
        else:
            # Composite cursor: created_at alone ties, and a tie drops or repeats a row.
            cur = await self.pool.fetchrow(
                "SELECT created_at, id FROM chat_thread WHERE id = $1 AND user_id = $2",
                after,
                context.user_id,
            )
            if cur is None:
                raise NotFoundError(f"Thread {after} not found")
            rows = await self.pool.fetch(
                f"SELECT id, payload FROM chat_thread WHERE user_id = $1 "
                f"  AND (created_at, id) {cmp_op} ($3, $4) "
                f"ORDER BY created_at {direction}, id {direction} LIMIT $2",
                context.user_id,
                limit + 1,
                cur["created_at"],
                cur["id"],
            )
        return _page(rows, limit, ThreadMetadata.model_validate_json)

    async def delete_thread(self, thread_id: str, context: RequestContext) -> None:
        got = await self.pool.fetchval(
            "DELETE FROM chat_thread WHERE id = $1 AND user_id = $2 RETURNING id",
            thread_id,
            context.user_id,
        )
        if got is None:
            raise NotFoundError(f"Thread {thread_id} not found")
        # chat_thread_item rows go with it via ON DELETE CASCADE.

    # ---- items -------------------------------------------------------------
    async def load_thread_items(
        self,
        thread_id: str,
        after: str | None,
        limit: int,
        order: str,
        context: RequestContext,
    ) -> Page[ThreadItem]:
        # The ownership check is explicit here because `items.list` reaches this method with
        # a client-supplied thread_id and no prior load_thread. Without it, user B asking for
        # user A's thread would get an empty 200 instead of a 404.
        await self._owned_thread(thread_id, context)
        cmp_op, direction = (">", "ASC") if order == "asc" else ("<", "DESC")
        if after is None:
            rows = await self.pool.fetch(
                f"SELECT id, payload FROM chat_thread_item "
                f"WHERE thread_id = $1 AND user_id = $2 ORDER BY seq {direction} LIMIT $3",
                thread_id,
                context.user_id,
                limit + 1,
            )
        else:
            cur = await self.pool.fetchval(
                "SELECT seq FROM chat_thread_item "
                "WHERE thread_id = $1 AND id = $2 AND user_id = $3",
                thread_id,
                after,
                context.user_id,
            )
            if cur is None:
                raise NotFoundError(f"Item {after} not found")
            rows = await self.pool.fetch(
                f"SELECT id, payload FROM chat_thread_item "
                f"WHERE thread_id = $1 AND user_id = $2 AND seq {cmp_op} $4 "
                f"ORDER BY seq {direction} LIMIT $3",
                thread_id,
                context.user_id,
                limit + 1,
                cur,
            )
        return _page(rows, limit, _ITEM.validate_json)

    async def add_thread_item(
        self, thread_id: str, item: ThreadItem, context: RequestContext
    ) -> None:
        # INSERT … SELECT proves ownership of the parent thread in the SAME statement, and
        # copies user_id off the thread row rather than trusting a second parameter.
        # `seq` is omitted so the identity column assigns it.
        got = await self.pool.fetchval(
            """
            INSERT INTO chat_thread_item (id, thread_id, user_id, type, payload, created_at)
            SELECT $2, t.id, t.user_id, $3, $4::jsonb, $5
              FROM chat_thread t
             WHERE t.id = $1 AND t.user_id = $6
            ON CONFLICT (id) DO NOTHING
            RETURNING id
            """,
            thread_id,
            item.id,
            item.type,
            _dump(item),
            _aware(item.created_at),
            context.user_id,
        )
        if got is None:
            # Either the thread is not yours, or the item id already exists (unreachable
            # with 144-bit ids, and a bug if it happens). Same answer either way.
            raise NotFoundError(f"Thread {thread_id} not found")

    async def save_item(self, thread_id: str, item: ThreadItem, context: RequestContext) -> None:
        # Documented as "Upsert a thread item by id." It MUST be an upsert: a bare UPDATE
        # silently no-ops for an id that is not yet stored, and this is the path
        # ThreadItemReplacedEvent takes — which is how a structured-input answer is
        # recorded. The official sample stores get this wrong.
        got = await self.pool.fetchval(
            """
            INSERT INTO chat_thread_item (id, thread_id, user_id, type, payload, created_at)
            SELECT $2, t.id, t.user_id, $3, $4::jsonb, $5
              FROM chat_thread t
             WHERE t.id = $1 AND t.user_id = $6
            ON CONFLICT (id) DO UPDATE
               SET type = EXCLUDED.type, payload = EXCLUDED.payload
             WHERE chat_thread_item.user_id = $6 AND chat_thread_item.thread_id = $1
            RETURNING id
            """,
            thread_id,
            item.id,
            item.type,
            _dump(item),
            _aware(item.created_at),
            context.user_id,
        )
        if got is None:
            raise NotFoundError(f"Item {item.id} not found")

    async def load_item(self, thread_id: str, item_id: str, context: RequestContext) -> ThreadItem:
        payload = await self.pool.fetchval(
            "SELECT payload FROM chat_thread_item "
            "WHERE thread_id = $1 AND id = $2 AND user_id = $3",
            thread_id,
            item_id,
            context.user_id,
        )
        if payload is None:
            raise NotFoundError(f"Item {item_id} not found")
        return _ITEM.validate_json(payload)

    async def delete_thread_item(
        self, thread_id: str, item_id: str, context: RequestContext
    ) -> None:
        got = await self.pool.fetchval(
            "DELETE FROM chat_thread_item "
            "WHERE thread_id = $1 AND id = $2 AND user_id = $3 RETURNING id",
            thread_id,
            item_id,
            context.user_id,
        )
        if got is None:
            raise NotFoundError(f"Item {item_id} not found")

    # ---- attachments -------------------------------------------------------
    # v2 ships with uploads OFF (attachment_store=None), so these are never called. They are
    # still @abstractmethod on Store — the class will not instantiate without them. Scoped
    # properly rather than stubbed with `pass`: a `pass` stub is a latent cross-user read the
    # day anyone turns attachments on.
    async def save_attachment(self, attachment: Attachment, context: RequestContext) -> None:
        got = await self.pool.fetchval(
            """
            INSERT INTO chat_attachment (id, user_id, payload)
            VALUES ($1, $2, $3::jsonb)
            ON CONFLICT (id) DO UPDATE SET payload = EXCLUDED.payload
             WHERE chat_attachment.user_id = $2
            RETURNING id
            """,
            attachment.id,
            context.user_id,
            _dump(attachment),
        )
        if got is None:
            raise NotFoundError(f"Attachment {attachment.id} not found")

    async def load_attachment(self, attachment_id: str, context: RequestContext) -> Attachment:
        payload = await self.pool.fetchval(
            "SELECT payload FROM chat_attachment WHERE id = $1 AND user_id = $2",
            attachment_id,
            context.user_id,
        )
        if payload is None:
            raise NotFoundError(f"Attachment {attachment_id} not found")
        return _ATTACHMENT.validate_json(payload)

    async def delete_attachment(self, attachment_id: str, context: RequestContext) -> None:
        got = await self.pool.fetchval(
            "DELETE FROM chat_attachment WHERE id = $1 AND user_id = $2 RETURNING id",
            attachment_id,
            context.user_id,
        )
        if got is None:
            raise NotFoundError(f"Attachment {attachment_id} not found")


def _page[T](rows: Sequence[Any], limit: int, parse: Callable[[Any], T]) -> Page[T]:
    """limit + 1 rows were fetched; the extra one is the has_more probe.

    `after` is an item-id cursor, not an offset, and is set ONLY when there is more —
    `_paginate_thread_items_reverse` loops until has_more is false and feeds `after` back in.
    """
    has_more = len(rows) > limit
    rows = rows[:limit]
    return Page(
        data=[parse(r["payload"]) for r in rows],
        has_more=has_more,
        after=(rows[-1]["id"] if has_more and rows else None),
    )
