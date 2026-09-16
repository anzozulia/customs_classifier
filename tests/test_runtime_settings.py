"""The override layer, proved against a real SQL engine.

Same technique as `tests/test_store_isolation.py`: no Postgres and no network, but the SQL
is EXECUTED — by SQLite, through a translating asyncpg-shaped double — rather than matched
against a hand-rolled dict. The `ON CONFLICT (key) DO UPDATE` upsert in `set_runtime` is the
reason that matters here: a double that just assigned into a dict would keep passing after
someone broke the conflict target, and the upsert is the only write path this table has.

The three properties under test, in order of how much they cost to get wrong:

  1. **Empty table means private.** The default is not read from the environment, so no
     stale line in a .env file can open this deployment to the internet.
  2. **A bad value is rejected at WRITE time** — with nothing written — because that is the
     only moment a human is still around to be told why.
  3. **A bad value is survived at READ time.** `app_setting` is one `psql` session away from
     holding `reasoning_effort = 'ultra'`, and that must degrade this app to its environment
     configuration, not break every turn in it.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator
from typing import Any, cast

import asyncpg
import pytest

from app import db as db_module
from app import runtime_settings as rs
from app.settings import Settings

# Mirrors migration 0005's app_setting, with PG types swapped for SQLite ones. JSONB is TEXT
# here, which is exactly what asyncpg hands back for a JSONB column too (app/db.py installs
# no jsonb codec), so `_parse_value` sees the same `str` in both worlds.
_SCHEMA = """
CREATE TABLE app_user (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT    NOT NULL UNIQUE
);

CREATE TABLE app_setting (
    key        TEXT    PRIMARY KEY,
    value      TEXT    NOT NULL,
    updated_at TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_by INTEGER REFERENCES app_user(id) ON DELETE SET NULL
);

INSERT INTO app_user (id, username) VALUES (1, 'root');
"""

ADMIN_ID = 1

_PARAM = re.compile(r"\$(\d+)")


def _translate(sql: str, args: tuple[Any, ...]) -> tuple[str, list[Any]]:
    collected: list[Any] = []

    def _sub(match: re.Match[str]) -> str:
        collected.append(args[int(match.group(1)) - 1])
        return "?"

    sql = _PARAM.sub(_sub, sql)
    return sql.replace("::jsonb", "").replace("now()", "CURRENT_TIMESTAMP"), collected


class FakePool:
    """The three asyncpg Pool methods this module uses, over in-memory SQLite."""

    def __init__(self) -> None:
        self._conn = sqlite3.connect(":memory:")
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self.statements: list[str] = []
        self.fail = False

    def _run(self, sql: str, args: tuple[Any, ...]) -> list[sqlite3.Row]:
        if self.fail:
            raise asyncpg.PostgresConnectionError("database is down")
        self.statements.append(" ".join(sql.split()))
        translated, params = _translate(sql, args)
        cursor = self._conn.execute(translated, params)
        rows = cursor.fetchall()
        self._conn.commit()
        return rows

    async def fetch(self, sql: str, *args: Any) -> list[sqlite3.Row]:
        return self._run(sql, args)

    async def fetchrow(self, sql: str, *args: Any) -> sqlite3.Row | None:
        rows = self._run(sql, args)
        return rows[0] if rows else None

    async def execute(self, sql: str, *args: Any) -> str:
        self._run(sql, args)
        return "OK"

    # -- test helpers, not part of the asyncpg surface ---------------------------------
    def raw_insert(self, key: str, value: str) -> None:
        """Write a value the way a human with psql would: no validation at all."""
        self._conn.execute(
            "INSERT INTO app_setting (key, value, updated_by) VALUES (?, ?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, value, ADMIN_ID),
        )
        self._conn.commit()

    def stored(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM app_setting WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def row_count(self) -> int:
        return int(self._conn.execute("SELECT count(*) AS n FROM app_setting").fetchone()["n"])

    def selects(self, needle: str) -> int:
        return sum(1 for s in self.statements if s.startswith("SELECT") and needle in s)


@pytest.fixture
def pool(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakePool]:
    """Install the fake as the process-wide pool and start every test with a cold cache.

    Patching `app.db._pool` rather than each importer's `get_pool` reference is what makes
    one line cover `runtime_settings`, `auth.deps` and `auth.guest` at once: they all call
    `get_pool()` at request time, and it reads this module global.
    """
    fake = FakePool()
    monkeypatch.setattr(db_module, "_pool", cast(asyncpg.Pool, fake))
    rs.invalidate_cache()
    yield fake
    rs.invalidate_cache()


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> Settings:
    """A known environment layer, so "falls back to the default" asserts against a value
    this file chose rather than against whatever .env happens to say today."""
    settings = Settings(model="gpt-test-terra", reasoning_effort="medium")
    monkeypatch.setattr(rs, "get_settings", lambda: settings)
    return settings


# --------------------------------------------------------------------------------------
# 1. Fail closed
# --------------------------------------------------------------------------------------


async def test_access_mode_is_private_when_the_table_is_empty(pool: FakePool) -> None:
    assert pool.row_count() == 0
    assert await rs.get_access_mode() == "private"
    assert await rs.get_runtime("access_mode") == "private"


async def test_access_mode_default_ignores_the_environment(
    pool: FakePool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """There is no ACCESS_MODE env var, and adding one must not become a way in.

    `Settings` has no such field, so this asserts the only thing that could go wrong: that
    the default is the module constant and nothing else.
    """
    monkeypatch.setenv("ACCESS_MODE", "public")
    assert rs.DEFAULT_ACCESS_MODE == "private"
    assert await rs.get_access_mode() == "private"


async def test_model_and_effort_default_to_the_environment(pool: FakePool, env: Settings) -> None:
    assert await rs.get_runtime("model") == "gpt-test-terra"
    assert await rs.get_runtime("reasoning_effort") == "medium"


async def test_a_dead_database_fails_closed(pool: FakePool) -> None:
    """If the database cannot answer, the access mode is private. The demo goes dark rather
    than open; every other failure mode in this app is already a 500 by then anyway."""
    pool.fail = True
    assert await rs.get_access_mode() == "private"
    assert await rs.get_runtime("model")  # resolves to the env default rather than raising


# --------------------------------------------------------------------------------------
# 2. Rejected at write time
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("access_mode", "PUBLIC"),  # case matters; the Literal is lowercase
        ("access_mode", "open"),
        ("access_mode", ""),
        ("access_mode", "public; DROP TABLE app_user"),
        ("reasoning_effort", "ultra"),
        ("reasoning_effort", "Low"),
        ("reasoning_effort", "0"),
        ("model", ""),
        ("model", "   "),
        ("model", "gpt 5"),  # a space
        ("model", "gpt-5/../etc/passwd"),
        ("model", "gpt-5\nmodel: evil"),
        ("model", "-leading-dash"),
        ("model", "x" * 200),
        ("model", None),
        ("model", 7),
        ("model", ["gpt-5"]),
    ],
)
async def test_set_runtime_rejects_a_bad_value_and_writes_nothing(
    pool: FakePool, key: str, value: Any
) -> None:
    with pytest.raises(rs.InvalidRuntimeValueError):
        await rs.set_runtime(cast(rs.RuntimeKey, key), value, updated_by=ADMIN_ID)
    assert pool.row_count() == 0


async def test_set_runtime_rejects_an_unknown_key(pool: FakePool) -> None:
    with pytest.raises(rs.InvalidRuntimeValueError):
        await rs.set_runtime(cast(rs.RuntimeKey, "max_turns"), "99", updated_by=ADMIN_ID)
    assert pool.row_count() == 0


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("access_mode", "public"),
        ("access_mode", "private"),
        ("reasoning_effort", "low"),
        ("reasoning_effort", "high"),
        ("model", "gpt-5.6-terra"),
        ("model", "o4-mini"),
        ("model", "gpt-4o-2024-08-06"),
        ("model", "ft:gpt-4o:acme::9aBcD"),
    ],
)
async def test_set_runtime_accepts_the_real_shapes(pool: FakePool, key: str, value: str) -> None:
    await rs.set_runtime(cast(rs.RuntimeKey, key), value, updated_by=ADMIN_ID)
    assert await rs.get_runtime(cast(rs.RuntimeKey, key)) == value


async def test_set_runtime_round_trips_and_records_who(pool: FakePool, env: Settings) -> None:
    await rs.set_runtime("model", "  o4-mini  ", updated_by=ADMIN_ID)  # padding is trimmed
    assert pool.stored("model") == json.dumps("o4-mini")

    resolved = (await rs.get_all_runtime())["model"]
    assert resolved.value == "o4-mini"
    assert resolved.source == "db"
    assert resolved.updated_by == ADMIN_ID
    assert resolved.updated_at is not None


async def test_set_runtime_upserts_rather_than_duplicating(pool: FakePool) -> None:
    await rs.set_runtime("access_mode", "public", updated_by=ADMIN_ID)
    await rs.set_runtime("access_mode", "private", updated_by=ADMIN_ID)
    assert pool.row_count() == 1
    assert await rs.get_access_mode() == "private"


# --------------------------------------------------------------------------------------
# 3. Survived at read time
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "corrupt", "expected"),
    [
        ("access_mode", '"PUBLIC"', "private"),
        ("access_mode", '"yes"', "private"),
        ("access_mode", "true", "private"),
        ("access_mode", "null", "private"),
        ("reasoning_effort", '"ultra"', "medium"),
        ("reasoning_effort", "3", "medium"),
        ("model", '""', "gpt-test-terra"),
        ("model", '"gpt 5 with spaces"', "gpt-test-terra"),
        ("model", '{"name": "gpt-5"}', "gpt-test-terra"),
    ],
)
async def test_a_hand_edited_row_falls_back_to_the_default(
    pool: FakePool, env: Settings, key: str, corrupt: str, expected: str
) -> None:
    pool.raw_insert(key, corrupt)
    assert await rs.get_runtime(cast(rs.RuntimeKey, key)) == expected
    assert (await rs.get_all_runtime())[cast(rs.RuntimeKey, key)].source == "default"


async def test_a_row_that_is_not_json_at_all_falls_back(pool: FakePool, env: Settings) -> None:
    """JSONB cannot hold this in Postgres, but the read path must not assume that."""
    pool.raw_insert("model", "not json at all")
    assert await rs.get_runtime("model") == "gpt-test-terra"
    assert await rs.get_access_mode() == "private"


async def test_a_corrupt_access_mode_row_does_not_open_the_demo(pool: FakePool) -> None:
    pool.raw_insert("access_mode", '"publik"')
    assert await rs.get_access_mode() == "private"


async def test_garbage_in_the_environment_falls_back_to_the_field_default(
    pool: FakePool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The env is configuration too. REASONING_EFFORT=ultra is the same bug as a bad row,
    and the floor beneath both is the literal declared on `Settings`."""
    monkeypatch.setattr(rs, "get_settings", lambda: Settings(reasoning_effort="ultra", model="!!"))
    assert await rs.get_runtime("reasoning_effort") == "low"
    assert await rs.get_runtime("model") == "gpt-5.6-terra"


# --------------------------------------------------------------------------------------
# Resolution surface and caching
# --------------------------------------------------------------------------------------


async def test_get_all_runtime_returns_every_key_with_its_source(
    pool: FakePool, env: Settings
) -> None:
    await rs.set_runtime("access_mode", "public", updated_by=ADMIN_ID)

    everything = await rs.get_all_runtime()
    assert set(everything) == set(rs.RUNTIME_KEYS)
    assert everything["access_mode"].value == "public"
    assert everything["access_mode"].source == "db"
    assert everything["model"].value == "gpt-test-terra"
    assert everything["model"].source == "default"
    assert everything["model"].updated_by is None
    assert everything["reasoning_effort"].source == "default"


async def test_model_reads_are_served_from_the_ttl_cache(pool: FakePool, env: Settings) -> None:
    await rs.get_runtime("model")
    before = pool.selects("FROM app_setting")
    for _ in range(20):
        await rs.get_runtime("model")
        await rs.get_runtime("reasoning_effort")
    assert pool.selects("FROM app_setting") == before, "hot-path reads hit the database"


async def test_the_cache_expires(
    pool: FakePool, env: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = [1000.0]
    monkeypatch.setattr(rs.time, "monotonic", lambda: clock[0])

    assert await rs.get_runtime("model") == "gpt-test-terra"
    pool.raw_insert("model", '"o4-mini"')
    assert await rs.get_runtime("model") == "gpt-test-terra", "still cached"

    clock[0] += rs._CACHE_TTL_S + 0.001
    assert await rs.get_runtime("model") == "o4-mini", "converged after the TTL"


async def test_set_runtime_invalidates_the_cache_immediately(
    pool: FakePool, env: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The superuser who just saved must see the new value, not the TTL's leftovers."""
    monkeypatch.setattr(rs.time, "monotonic", lambda: 1000.0)  # a clock that never advances

    assert await rs.get_runtime("model") == "gpt-test-terra"
    await rs.set_runtime("model", "o4-mini", updated_by=ADMIN_ID)
    assert await rs.get_runtime("model") == "o4-mini"


async def test_invalidate_cache_is_safe_to_call_twice(pool: FakePool, env: Settings) -> None:
    await rs.get_runtime("model")
    rs.invalidate_cache()
    rs.invalidate_cache()
    assert await rs.get_runtime("model") == "gpt-test-terra"


async def test_access_mode_is_never_served_from_the_cache(
    pool: FakePool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The kill switch reads the database every single time.

    This is the property that makes "flip it to private and guest traffic stops" true on the
    NEXT request in EVERY worker, instead of "within three seconds, per worker". The frozen
    clock proves the TTL is not involved: with time standing still, a cached read could
    never expire, and yet the second call sees the new value.
    """
    monkeypatch.setattr(rs.time, "monotonic", lambda: 1000.0)

    await rs.get_runtime("model")  # warm the shared snapshot cache
    assert await rs.get_access_mode() == "private"
    before = pool.selects("app_setting")

    pool.raw_insert("access_mode", '"public"')
    assert await rs.get_access_mode() == "public"
    assert pool.selects("app_setting") > before, "access_mode was answered from cache"


async def test_get_runtime_rejects_an_unknown_key(pool: FakePool) -> None:
    with pytest.raises(rs.InvalidRuntimeValueError):
        await rs.get_runtime(cast(rs.RuntimeKey, "database_url"))
