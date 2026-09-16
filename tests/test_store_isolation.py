"""THE test: two users, fourteen Store methods, and no way across.

`ChatKitServer` does no authorization at all — it hands `PgStore` a thread id that came
straight off the wire. So this file is the proof that the boundary holds, and it covers
WRITES as well as reads: `save_thread`, `save_item` and `add_thread_item` are the three a
naive implementation gets wrong, because "not found" on a read is obvious and "silently
updated someone else's row" is not.

It runs WITHOUT Postgres. `FakePool` is an asyncpg-shaped double backed by real SQLite
that EXECUTES the store's real SQL (`$n` → `?`, `::jsonb` and `now()` translated away).
That matters: a hand-rolled dict double would only ever test the double, and would keep
passing after someone deleted `AND user_id = $2`. Here the predicates are evaluated by an
actual SQL engine, so deleting one makes these tests fail.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from typing import Any, cast

import asyncpg
import pytest
from chatkit.store import NotFoundError
from chatkit.types import (
    FileAttachment,
    InferenceOptions,
    Page,
    Thread,
    ThreadItem,
    ThreadMetadata,
    UserMessageItem,
    UserMessageTextContent,
)

from app.chat.store import PgStore
from app.context import RequestContext

ALICE_ID = 1
BOB_ID = 2
THREAD_A = "thr_alice_thread"
ITEM_A = "msg_alice_item"
ATTACHMENT_A = "atc_alice_file"

# Mirrors migrations/versions/0002_chatkit_store.py, with PG types swapped for SQLite ones.
# `seq INTEGER PRIMARY KEY AUTOINCREMENT` is SQLite's spelling of the identity column: a
# database-assigned monotonic counter, which is exactly the semantics the migration relies on.
_SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE app_user (
    id       INTEGER PRIMARY KEY,
    username TEXT    NOT NULL UNIQUE
);

CREATE TABLE chat_thread (
    id         TEXT    PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    title      TEXT,
    metadata   TEXT    NOT NULL DEFAULT '{}',
    payload    TEXT    NOT NULL,
    created_at TEXT    NOT NULL,
    updated_at TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE chat_thread_item (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    id         TEXT    NOT NULL UNIQUE,
    thread_id  TEXT    NOT NULL REFERENCES chat_thread(id) ON DELETE CASCADE,
    user_id    INTEGER NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    type       TEXT    NOT NULL,
    payload    TEXT    NOT NULL,
    created_at TEXT    NOT NULL,
    UNIQUE (thread_id, seq)
);

CREATE TABLE chat_attachment (
    id         TEXT    PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    payload    TEXT    NOT NULL,
    created_at TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
);

INSERT INTO app_user (id, username) VALUES (1, 'alice'), (2, 'bob');
"""

_PARAM = re.compile(r"\$(\d+)")
_CHAT_TABLE = re.compile(r"\bchat_(thread|thread_item|attachment)\b")


def _translate(sql: str, args: tuple[Any, ...]) -> tuple[str, list[Any]]:
    """asyncpg dialect → sqlite3 dialect.

    `$n` may repeat and appear out of order (save_thread uses `$2` twice), so parameters are
    collected in the order the placeholders appear rather than by index.
    """
    collected: list[Any] = []

    def _sub(match: re.Match[str]) -> str:
        collected.append(args[int(match.group(1)) - 1])
        return "?"

    sql = _PARAM.sub(_sub, sql)
    return sql.replace("::jsonb", "").replace("now()", "CURRENT_TIMESTAMP"), collected


class FakePool:
    """The four asyncpg Pool methods PgStore uses, over an in-memory SQLite database."""

    def __init__(self) -> None:
        self._conn = sqlite3.connect(":memory:")
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self.statements: list[str] = []

    def _run(self, sql: str, args: tuple[Any, ...]) -> list[sqlite3.Row]:
        # The structural invariant, enforced on every statement the store issues: any query
        # that touches a chat_* table MUST constrain user_id. This is what stops an unscoped
        # query from being added later under a test that does not happen to cover it.
        if _CHAT_TABLE.search(sql) and "user_id" not in sql:
            raise AssertionError(f"unscoped query — no user_id predicate:\n{sql}")
        self.statements.append(sql)
        translated, params = _translate(sql, args)
        cursor = self._conn.execute(translated, params)
        rows = cursor.fetchall()  # drain before commit: RETURNING leaves the cursor open
        self._conn.commit()
        return rows

    async def fetch(self, sql: str, *args: Any) -> list[sqlite3.Row]:
        return self._run(sql, args)

    async def fetchrow(self, sql: str, *args: Any) -> sqlite3.Row | None:
        rows = self._run(sql, args)
        return rows[0] if rows else None

    async def fetchval(self, sql: str, *args: Any) -> Any:
        rows = self._run(sql, args)
        return rows[0][0] if rows else None

    async def execute(self, sql: str, *args: Any) -> str:
        self._run(sql, args)
        return "OK"


@pytest.fixture
def store() -> PgStore:
    # cast: FakePool is duck-typed, and PgStore only ever calls the four methods above.
    return PgStore(cast(asyncpg.Pool, FakePool()))


@pytest.fixture
def alice() -> RequestContext:
    return RequestContext(user_id=ALICE_ID, request_id="req-alice")


@pytest.fixture
def bob() -> RequestContext:
    return RequestContext(user_id=BOB_ID, request_id="req-bob")


def _thread(
    thread_id: str = THREAD_A, *, minute: int = 0, title: str | None = None
) -> ThreadMetadata:
    # Naive local time is what ChatKit itself produces (`datetime.now()`), so the store's
    # timezone handling is exercised rather than side-stepped.
    return ThreadMetadata(
        id=thread_id, title=title, created_at=datetime(2026, 9, 16, 12, minute, 0)
    )


def _message(item_id: str, thread_id: str = THREAD_A, *, text: str = "чайник") -> UserMessageItem:
    return UserMessageItem(
        id=item_id,
        thread_id=thread_id,
        created_at=datetime(2026, 9, 16, 12, 0, 0),
        content=[UserMessageTextContent(text=text)],
        inference_options=InferenceOptions(),
    )


def _attachment(attachment_id: str = ATTACHMENT_A) -> FileAttachment:
    return FileAttachment(id=attachment_id, name="spec.pdf", mime_type="application/pdf")


async def _seed_alice(store: PgStore, alice: RequestContext) -> None:
    await store.save_thread(_thread(), alice)
    await store.add_thread_item(THREAD_A, _message(ITEM_A), alice)
    await store.save_attachment(_attachment(), alice)


# --------------------------------------------------------------------------------------
# The isolation proof
# --------------------------------------------------------------------------------------


async def test_user_b_cannot_read_or_write_user_a(
    store: PgStore, alice: RequestContext, bob: RequestContext
) -> None:
    await _seed_alice(store, alice)

    attacks = {
        "load_thread": lambda ctx: store.load_thread(THREAD_A, ctx),
        "save_thread": lambda ctx: store.save_thread(_thread(title="pwned"), ctx),
        "delete_thread": lambda ctx: store.delete_thread(THREAD_A, ctx),
        "load_thread_items": lambda ctx: store.load_thread_items(THREAD_A, None, 20, "asc", ctx),
        "load_thread_items_cursor": lambda ctx: store.load_thread_items(
            THREAD_A, ITEM_A, 20, "asc", ctx
        ),
        "load_item": lambda ctx: store.load_item(THREAD_A, ITEM_A, ctx),
        "save_item": lambda ctx: store.save_item(THREAD_A, _message(ITEM_A, text="pwned"), ctx),
        "add_thread_item": lambda ctx: store.add_thread_item(THREAD_A, _message("msg_bob"), ctx),
        "delete_thread_item": lambda ctx: store.delete_thread_item(THREAD_A, ITEM_A, ctx),
        "load_threads_cursor": lambda ctx: store.load_threads(20, THREAD_A, "desc", ctx),
        "save_attachment": lambda ctx: store.save_attachment(_attachment(), ctx),
        "load_attachment": lambda ctx: store.load_attachment(ATTACHMENT_A, ctx),
        "delete_attachment": lambda ctx: store.delete_attachment(ATTACHMENT_A, ctx),
    }
    for name, attack in attacks.items():
        # NotFoundError — never a 403. "Not yours" and "does not exist" must be the same
        # answer, or an authenticated session becomes an existence oracle.
        try:
            await attack(bob)
        except NotFoundError:
            continue
        pytest.fail(f"{name} did not raise NotFoundError for a foreign user")

    # load_threads is the fourteenth read path: it cannot raise, so it must return nothing.
    assert (await store.load_threads(20, None, "desc", bob)).data == []

    # …and every one of those attacks left Alice's data exactly as it was.
    assert (await store.load_thread(THREAD_A, alice)).title is None
    items = await store.load_thread_items(THREAD_A, None, 20, "asc", alice)
    assert [i.id for i in items.data] == [ITEM_A]
    assert cast(UserMessageItem, items.data[0]).content[0].text == "чайник"
    assert (await store.load_attachment(ATTACHMENT_A, alice)).name == "spec.pdf"


async def test_the_same_methods_work_for_the_owner(store: PgStore, alice: RequestContext) -> None:
    """The mirror image: proof the attacks above fail because of the user_id predicate and
    not because the calls are broken for everybody."""
    await _seed_alice(store, alice)

    assert (await store.load_thread(THREAD_A, alice)).id == THREAD_A
    assert (await store.load_item(THREAD_A, ITEM_A, alice)).id == ITEM_A
    assert (await store.load_threads(20, None, "desc", alice)).data[0].id == THREAD_A
    assert (await store.load_attachment(ATTACHMENT_A, alice)).id == ATTACHMENT_A

    await store.save_thread(_thread(title="Чайники"), alice)
    assert (await store.load_thread(THREAD_A, alice)).title == "Чайники"

    await store.add_thread_item(THREAD_A, _message("msg_second"), alice)
    await store.save_item(THREAD_A, _message(ITEM_A, text="оновлено"), alice)
    await store.delete_thread_item(THREAD_A, "msg_second", alice)
    await store.delete_attachment(ATTACHMENT_A, alice)
    await store.delete_thread(THREAD_A, alice)

    with pytest.raises(NotFoundError):
        await store.load_thread(THREAD_A, alice)


async def test_bob_owns_his_own_threads(
    store: PgStore, alice: RequestContext, bob: RequestContext
) -> None:
    """Isolation is symmetric, and it is not "alice can do everything"."""
    await _seed_alice(store, alice)
    await store.save_thread(_thread("thr_bob", minute=5), bob)
    await store.add_thread_item("thr_bob", _message("msg_bob", "thr_bob"), bob)

    assert [t.id for t in (await store.load_threads(20, None, "desc", bob)).data] == ["thr_bob"]
    assert [t.id for t in (await store.load_threads(20, None, "desc", alice)).data] == [THREAD_A]
    with pytest.raises(NotFoundError):
        await store.load_thread("thr_bob", alice)


async def test_user_b_cannot_hijack_an_item_id_through_his_own_thread(
    store: PgStore, alice: RequestContext, bob: RequestContext
) -> None:
    """The nastier shape of the same attack: the item id space is GLOBAL (id is the primary
    key of chat_thread_item), so an attacker who legitimately owns a thread can aim an
    upsert at someone else's item id. The parent-thread predicate alone does not stop that —
    the thread in the statement is genuinely his. What stops it is the guard on save_item's
    DO UPDATE, and `DO NOTHING` on add_thread_item."""
    await _seed_alice(store, alice)
    await store.save_thread(_thread("thr_bob", minute=5), bob)

    with pytest.raises(NotFoundError):
        await store.save_item("thr_bob", _message(ITEM_A, "thr_bob", text="pwned"), bob)
    with pytest.raises(NotFoundError):
        await store.add_thread_item("thr_bob", _message(ITEM_A, "thr_bob", text="pwned"), bob)

    survivor = cast(UserMessageItem, await store.load_item(THREAD_A, ITEM_A, alice))
    assert survivor.content[0].text == "чайник"
    assert survivor.thread_id == THREAD_A


# --------------------------------------------------------------------------------------
# The bugs this store exists to not have
# --------------------------------------------------------------------------------------


async def test_ids_are_not_enumerable(store: PgStore, alice: RequestContext) -> None:
    """ChatKit's default ids are uuid4().hex[:8] — 32 bits, trivially enumerated from an
    authenticated session, and thread ids travel in client requests."""
    thread_id = store.generate_thread_id(alice)
    assert thread_id.startswith("thr_")
    assert len(thread_id) >= len("thr_") + 32
    assert len({store.generate_thread_id(alice) for _ in range(100)}) == 100

    item_id = store.generate_item_id("message", _thread(), alice)
    assert item_id.startswith("msg_")
    assert len(item_id) >= len("msg_") + 24
    assert len({store.generate_item_id("workflow", _thread(), alice) for _ in range(100)}) == 100


async def test_save_item_upserts_an_unsaved_id(store: PgStore, alice: RequestContext) -> None:
    """`save_item` is documented as an upsert and is reached through
    ThreadItemReplacedEvent. A bare UPDATE silently no-ops for an id that was never
    inserted — a real bug in the official sample stores, and the structured-input answer
    path depends on this working."""
    await store.save_thread(_thread(), alice)

    await store.save_item(THREAD_A, _message("msg_never_added"), alice)  # insert half
    assert (await store.load_item(THREAD_A, "msg_never_added", alice)).id == "msg_never_added"

    await store.save_item(THREAD_A, _message("msg_never_added", text="замінено"), alice)
    stored = cast(UserMessageItem, await store.load_item(THREAD_A, "msg_never_added", alice))
    assert stored.content[0].text == "замінено"
    assert len((await store.load_thread_items(THREAD_A, None, 20, "asc", alice)).data) == 1


async def test_save_thread_normalises_a_thread_subclass(
    store: PgStore, alice: RequestContext
) -> None:
    """On threads.create the SDK builds a `Thread` (ThreadMetadata + `items`) and
    `_process_events` saves THAT object, repeatedly, for the rest of the turn. Two things
    must hold: the item page is never written into the thread row, and load_thread returns
    something `_load_full_thread` can still do `Thread(**meta.model_dump(), items=…)` with —
    that reconstruction is what raises `TypeError: got multiple values for keyword argument
    'items'` in both official in-memory stores, on every threads.get_by_id, as soon as a
    title is set (which the history feature requires)."""
    empty: Page[ThreadItem] = Page[ThreadItem]()
    await store.save_thread(
        Thread(id=THREAD_A, title="Чайники", created_at=datetime(2026, 9, 16, 12, 0), items=empty),
        alice,
    )

    stored = json.loads(
        await cast(FakePool, store.pool).fetchval(
            "SELECT payload FROM chat_thread WHERE id = $1 AND user_id = $2", THREAD_A, ALICE_ID
        )
    )
    assert "items" not in stored

    loaded = await store.load_thread(THREAD_A, alice)
    assert type(loaded) is ThreadMetadata
    assert "items" not in loaded.model_dump()
    # The exact reconstruction _load_full_thread performs.
    assert Thread(**loaded.model_dump(), items=empty).title == "Чайники"


async def test_pagination_is_a_stable_cursor(store: PgStore, alice: RequestContext) -> None:
    """`after` is an item-id cursor, not an offset, and is set ONLY when there is more —
    `_paginate_thread_items_reverse` loops on has_more and feeds `after` straight back in.
    Ordering is by the database-assigned `seq`, so items created in the same microsecond
    (ChatKit stamps them with datetime.now()) still paginate deterministically."""
    await store.save_thread(_thread(), alice)
    for n in range(5):
        await store.add_thread_item(THREAD_A, _message(f"msg_{n}"), alice)

    page1 = await store.load_thread_items(THREAD_A, None, 2, "asc", alice)
    assert [i.id for i in page1.data] == ["msg_0", "msg_1"]
    assert page1.has_more and page1.after == "msg_1"

    page2 = await store.load_thread_items(THREAD_A, page1.after, 2, "asc", alice)
    assert [i.id for i in page2.data] == ["msg_2", "msg_3"]

    page3 = await store.load_thread_items(THREAD_A, page2.after, 2, "asc", alice)
    assert [i.id for i in page3.data] == ["msg_4"]
    assert not page3.has_more and page3.after is None

    # The SDK's own reverse page: (thread.id, None, 2, "desc", ctx) on every turn.
    newest = await store.load_thread_items(THREAD_A, None, 2, "desc", alice)
    assert [i.id for i in newest.data] == ["msg_4", "msg_3"]


async def test_thread_list_pagination(store: PgStore, alice: RequestContext) -> None:
    for n in range(3):
        await store.save_thread(_thread(f"thr_{n}", minute=n), alice)

    page1 = await store.load_threads(2, None, "desc", alice)
    assert [t.id for t in page1.data] == ["thr_2", "thr_1"]
    assert page1.has_more and page1.after == "thr_1"

    page2 = await store.load_threads(2, page1.after, "desc", alice)
    assert [t.id for t in page2.data] == ["thr_0"]
    assert not page2.has_more


async def test_delete_thread_takes_its_items(store: PgStore, alice: RequestContext) -> None:
    await _seed_alice(store, alice)
    await store.delete_thread(THREAD_A, alice)

    with pytest.raises(NotFoundError):
        await store.load_item(THREAD_A, ITEM_A, alice)
    with pytest.raises(NotFoundError):
        await store.load_thread_items(THREAD_A, None, 20, "asc", alice)


async def test_payloads_round_trip_through_json(store: PgStore, alice: RequestContext) -> None:
    """Items are stored as `model_dump(mode="json")` and rehydrated through the ThreadItem
    TypeAdapter — the union is discriminated on `type`, so the concrete class must come back."""
    await store.save_thread(_thread(), alice)
    await store.add_thread_item(THREAD_A, _message(ITEM_A), alice)

    loaded = await store.load_item(THREAD_A, ITEM_A, alice)
    assert isinstance(loaded, UserMessageItem)
    assert loaded.model_dump(mode="json") == _message(ITEM_A).model_dump(mode="json")

    raw = json.loads(
        await cast(FakePool, store.pool).fetchval(
            "SELECT payload FROM chat_thread_item WHERE id = $1 AND user_id = $2",
            ITEM_A,
            ALICE_ID,
        )
    )
    assert raw["type"] == "user_message"
