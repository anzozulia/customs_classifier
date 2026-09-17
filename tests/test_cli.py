"""THE test for the only door accounts come through — and the only command that deletes.

`app/cli.py` is not "just a CLI". There is no registration route anywhere in the app, so
this file is the entire account lifecycle for humans, it is what a deployer runs inside the
container on a VPS, and `purge-guests` is the one command in the codebase that issues a
DELETE. A stack trace here is a deploy that cannot proceed; an error printed with exit code
0 is worse, because every `set -e` script and every `if ! python -m app.cli …` in a Makefile
would sail straight past it. So EVERY failure path below asserts `exit_code`, not just the
message, and the two halves of "is_superuser" — set it, refuse to set it on a guest — are
asserted against the stored row rather than against what the command printed.

It runs WITHOUT Postgres, reusing the technique from `tests/test_store_isolation.py` and
`tests/test_records_writer.py`: `FakePool` is an asyncpg-shaped double over real SQLite that
EXECUTES the CLI's real SQL (`$n` → `?`, casts stripped, `now() - make_interval(days => …)`
rewritten to `datetime('now', '-n days')`), against a schema mirroring migrations 0001, 0002,
0004 and 0005 — including `username … COLLATE NOCASE UNIQUE` for CITEXT and the
`ON DELETE CASCADE` foreign keys `purge-guests` relies on. That is the point: the duplicate
username is rejected by an actual UNIQUE index, the day-window arithmetic is evaluated by an
actual SQL engine, and the guest's threads and classifications disappear because SQLite
really cascades them. A dict double would only ever test the double.

The pool is installed by monkeypatching `app.cli.init_pool` / `app.cli.close_pool`, so `_run`
— including `asyncio.run` and the `finally: close_pool()` — is the real one under test, and
`get_pool()` inside every command resolves to the SQLite-backed fake.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import asyncpg
import pytest
from click.testing import Result
from typer.testing import CliRunner

import app.cli as cli_mod
import app.db as app_db
from app.auth.passwords import verify_password
from app.cli import cli as cli_app
from app.tariff.ingest import IngestInvariantError, IngestResult

# Mirrors migrations 0001_users + 0005_admin (app_user), 0002_chatkit_store (chat_thread,
# chat_thread_item) and 0004_records (classification, classification_code), with PG types
# swapped for SQLite ones. Two details are load-bearing rather than decorative:
#
#   * `username TEXT COLLATE NOCASE UNIQUE` is SQLite's spelling of CITEXT + the unique
#     constraint. Case-insensitive uniqueness is a property of the schema in Postgres, and it
#     has to be a property of the schema here too, or `create-user ANTON` after `anton` would
#     "pass" in the test and create a second account in production.
#   * every `user_id` is `ON DELETE CASCADE`, exactly as the migrations declare it. The whole
#     safety argument for `purge-guests` is that one DELETE takes the threads, items,
#     classifications and per-code rows with it; here that is executed, not assumed.
_SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE app_user (
    id            INTEGER PRIMARY KEY,
    username      TEXT    NOT NULL COLLATE NOCASE UNIQUE,
    password_hash TEXT    NOT NULL,
    display_name  TEXT    NOT NULL DEFAULT '',
    is_active     BOOLEAN NOT NULL DEFAULT TRUE,
    is_reviewer   BOOLEAN NOT NULL DEFAULT FALSE,
    session_epoch INTEGER NOT NULL DEFAULT 0,
    created_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_login_at TIMESTAMP,
    kind          TEXT    NOT NULL DEFAULT 'human',
    is_superuser  BOOLEAN NOT NULL DEFAULT FALSE,
    last_seen_at  TIMESTAMP,
    CHECK (kind IN ('human', 'guest'))
);

CREATE TABLE chat_thread (
    id         TEXT    PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    title      TEXT,
    payload    TEXT    NOT NULL DEFAULT '{}',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE chat_thread_item (
    seq       INTEGER PRIMARY KEY AUTOINCREMENT,
    id        TEXT    NOT NULL UNIQUE,
    thread_id TEXT    NOT NULL REFERENCES chat_thread(id) ON DELETE CASCADE,
    user_id   INTEGER NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    payload   TEXT    NOT NULL DEFAULT '{}'
);

CREATE TABLE classification (
    id      TEXT    PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    outcome TEXT    NOT NULL DEFAULT 'classified'
);

CREATE TABLE classification_code (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    classification_id TEXT    NOT NULL REFERENCES classification(id) ON DELETE CASCADE,
    code              TEXT    NOT NULL
);
"""

_PARAM = re.compile(r"\$(\d+)")
_CAST = re.compile(r"::[a-z]+")
# `now() - make_interval(days => $1::int)` has no SQLite equivalent to strip down to; it has
# to be rewritten whole, before the generic cast strip eats the `::int` it matches on.
_INTERVAL = re.compile(r"now\(\)\s*-\s*make_interval\(days\s*=>\s*\$(\d+)::int\)")
_WRITE = re.compile(r"^\s*(UPDATE|DELETE)\b", re.IGNORECASE)

# asyncpg hands TIMESTAMPTZ back as `datetime`; `list-users` calls `.strftime()` on it. SQLite
# hands back text, so the columns the CLI reads as dates are rehydrated on the way out.
_TIMESTAMP_COLUMNS = frozenset({"created_at", "last_login_at", "last_seen_at"})


def _translate(sql: str, args: tuple[Any, ...]) -> tuple[str, list[Any]]:
    """asyncpg dialect → sqlite3 dialect."""
    sql = _INTERVAL.sub(lambda m: f"datetime('now', '-' || ${m.group(1)} || ' days')", sql)

    collected: list[Any] = []

    def _sub(match: re.Match[str]) -> str:
        collected.append(args[int(match.group(1)) - 1])
        return "?"

    sql = _PARAM.sub(_sub, sql)
    return _CAST.sub("", sql).replace("now()", "CURRENT_TIMESTAMP"), collected


def _row_factory(cursor: sqlite3.Cursor, values: tuple[Any, ...]) -> dict[str, Any]:
    row: dict[str, Any] = {}
    for column, value in zip((d[0] for d in cursor.description), values, strict=True):
        if column in _TIMESTAMP_COLUMNS and isinstance(value, str):
            value = datetime.fromisoformat(value)
        row[column] = value
    return row


def _days_ago(days: float) -> str:
    """A timestamp in SQLite's `datetime('now')` format — UTC, space-separated, which is what
    the day-window comparison in `purge-guests` is evaluated against."""
    return (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


class FakePool:
    """The asyncpg Pool surface `app/cli.py` uses, over an in-memory SQLite database."""

    def __init__(self) -> None:
        self._conn = sqlite3.connect(":memory:")
        self._conn.row_factory = _row_factory
        self._conn.executescript(_SCHEMA)
        self.statements: list[str] = []
        self.opened = 0
        self.closed = 0

    # -- the double ---------------------------------------------------------------------

    def _run(self, sql: str, args: tuple[Any, ...]) -> list[dict[str, Any]]:
        # Structural invariant, checked on every statement the CLI issues: an UPDATE or a
        # DELETE must carry a WHERE. `UPDATE app_user SET is_active = FALSE` with the
        # predicate lost in an edit disables every account in the deployment, and would
        # otherwise still satisfy an assertion about the one user the test looked at.
        if _WRITE.match(sql) and "WHERE" not in sql.upper():
            raise AssertionError(f"unscoped write — no WHERE clause:\n{sql}")
        self.statements.append(sql)
        translated, params = _translate(sql, args)
        try:
            cursor = self._conn.execute(translated, params)
        except sqlite3.IntegrityError as exc:  # what Postgres raises for the same collision
            if "UNIQUE" in str(exc):
                raise asyncpg.UniqueViolationError(str(exc)) from exc
            raise
        rows = cursor.fetchall()  # drain before commit: RETURNING leaves the cursor open
        self._conn.commit()
        return rows

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        return self._run(sql, args)

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        rows = self._run(sql, args)
        return rows[0] if rows else None

    async def fetchval(self, sql: str, *args: Any) -> Any:
        rows = self._run(sql, args)
        return next(iter(rows[0].values())) if rows else None

    async def execute(self, sql: str, *args: Any) -> str:
        self._run(sql, args)
        return "OK"

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[FakePool]:
        """`ingest` borrows a connection out of the pool; the fake is its own connection."""
        yield self

    def close(self) -> None:
        self._conn.close()

    # -- test-side helpers --------------------------------------------------------------

    def add_user(
        self,
        username: str,
        *,
        kind: str = "human",
        is_superuser: bool = False,
        is_active: bool = True,
        session_epoch: int = 0,
        password_hash: str = "$argon2id$seeded",
        created_days_ago: float = 0.0,
        last_seen_days_ago: float | None = None,
        last_login_days_ago: float | None = None,
    ) -> int:
        cursor = self._conn.execute(
            "INSERT INTO app_user (username, password_hash, display_name, kind, is_superuser,"
            " is_active, session_epoch, created_at, last_seen_at, last_login_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
            (
                username,
                password_hash,
                username,
                kind,
                is_superuser,
                is_active,
                session_epoch,
                _days_ago(created_days_ago),
                None if last_seen_days_ago is None else _days_ago(last_seen_days_ago),
                None if last_login_days_ago is None else _days_ago(last_login_days_ago),
            ),
        )
        user_id = cast(int, cursor.fetchone()["id"])
        self._conn.commit()
        return user_id

    def add_guests(self, count: int, *, prefix: str = "bulk", days_ago: float = 90) -> None:
        self._conn.executemany(
            "INSERT INTO app_user (username, password_hash, kind, last_seen_at)"
            " VALUES (?, '$argon2id$unusable', 'guest', ?)",
            [(f"{prefix}_{n}", _days_ago(days_ago)) for n in range(count)],
        )
        self._conn.commit()

    def add_thread(self, thread_id: str, user_id: int, *, items: int = 1) -> None:
        self._conn.execute(
            "INSERT INTO chat_thread (id, user_id) VALUES (?, ?)", (thread_id, user_id)
        )
        self._conn.executemany(
            "INSERT INTO chat_thread_item (id, thread_id, user_id) VALUES (?, ?, ?)",
            [(f"{thread_id}_item_{n}", thread_id, user_id) for n in range(items)],
        )
        self._conn.commit()

    def add_classification(self, classification_id: str, user_id: int) -> None:
        self._conn.execute(
            "INSERT INTO classification (id, user_id) VALUES (?, ?)", (classification_id, user_id)
        )
        self._conn.execute(
            "INSERT INTO classification_code (classification_id, code) VALUES (?, '8516710000')",
            (classification_id,),
        )
        self._conn.commit()

    def user(self, username: str) -> dict[str, Any] | None:
        cursor = self._conn.execute("SELECT * FROM app_user WHERE username = ?", (username,))
        return cursor.fetchone()

    def usernames(self, *, kind: str | None = None) -> list[str]:
        sql = "SELECT username FROM app_user"
        params: tuple[Any, ...] = ()
        if kind is not None:
            sql += " WHERE kind = ?"
            params = (kind,)
        return [r["username"] for r in self._conn.execute(sql + " ORDER BY id", params).fetchall()]

    def count(self, table: str) -> int:
        return cast(int, self._conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"])


def _stamp(db: FakePool, username: str, column: str) -> str:
    """The stored timestamp, rendered the way `list-users` renders it. Read back out of the
    row rather than recomputed, so the assertion cannot drift across a midnight boundary."""
    row = db.user(username)
    assert row is not None
    return cast(datetime, row[column]).strftime("%Y-%m-%d %H:%M")


@pytest.fixture
def db(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakePool]:
    """Install the SQLite-backed pool where `_run` would have opened a real one.

    `init_pool`/`close_pool` are replaced rather than `app.db._pool` being pinned, so the
    open/close lifecycle `_run` performs per command stays observable — `purge-guests` runs
    two separate commands' worth of it around its confirmation prompt.
    """
    pool = FakePool()

    async def _init_pool(dsn: str, **kwargs: Any) -> Any:
        pool.opened += 1
        app_db._pool = cast(asyncpg.Pool, pool)
        return app_db._pool

    async def _close_pool() -> None:
        pool.closed += 1
        app_db._pool = None

    monkeypatch.setattr(app_db, "_pool", None)  # also restores it after the test
    monkeypatch.setattr(cli_mod, "init_pool", _init_pool)
    monkeypatch.setattr(cli_mod, "close_pool", _close_pool)
    yield pool
    pool.close()
    # No command may leave a connection pool behind: the CLI process exits, but a leaked pool
    # means the `finally` in `_run` was skipped, which is how a hung `docker compose exec` and
    # a "Event loop is closed" traceback on every invocation start.
    assert pool.opened == pool.closed, "a command opened the pool and did not close it"


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _invoke(runner: CliRunner, args: Sequence[str], **kwargs: Any) -> Result:
    """Invoke the CLI, re-raising anything that was not a deliberate exit.

    CliRunner turns an unhandled exception into `exit_code == 1` as well, so without this a
    stack trace would satisfy every `assert result.exit_code == 1` below.
    """
    result = runner.invoke(cli_app, list(args), **kwargs)
    if result.exception is not None and not isinstance(result.exception, SystemExit):
        raise result.exception
    return result


# ------------------------------------------------------------------------------------
# create-user — the only INSERT into app_user in the codebase
# ------------------------------------------------------------------------------------


def test_create_user_makes_an_ordinary_human_account(db: FakePool, runner: CliRunner) -> None:
    """The default must be the least privileged thing: kind='human' (never 'guest', which is
    minted by the runtime and carries an unusable hash), and is_superuser FALSE."""
    result = _invoke(runner, ["create-user", "anton", "--password", "s3cret"])

    assert result.exit_code == 0
    row = db.user("anton")
    assert row is not None
    assert row["kind"] == "human"
    assert row["is_superuser"] == 0
    assert row["is_active"] == 1
    assert row["session_epoch"] == 0
    assert "created user 'anton'" in result.output
    assert f"id={row['id']}" in result.output


def test_create_user_superuser_flag_actually_sets_the_flag(db: FakePool, runner: CliRunner) -> None:
    """The first superuser can ONLY be made here — `POST /api/admin/users/{id}/superuser`
    already requires being one. If the flag silently does not reach the column, the admin
    panel is unreachable for the life of the deployment and the only fix is hand-written SQL.
    """
    assert _invoke(runner, ["create-user", "root", "--password", "x", "--superuser"]).exit_code == 0
    assert _invoke(runner, ["create-user", "plain", "--password", "x"]).exit_code == 0

    root, plain = db.user("root"), db.user("plain")
    assert root is not None and plain is not None
    assert root["is_superuser"] == 1
    assert plain["is_superuser"] == 0


def test_create_user_announces_which_kind_of_account_it_made(
    db: FakePool, runner: CliRunner
) -> None:
    """The deployer's only confirmation that `--superuser` was honoured is this line."""
    admin = _invoke(runner, ["create-user", "root", "--password", "x", "--superuser"])
    plain = _invoke(runner, ["create-user", "anton", "--password", "x"])

    assert "created superuser 'root'" in admin.output
    assert "created user 'anton'" in plain.output
    assert "superuser" not in plain.output


def test_create_user_generates_a_password_prints_it_once_and_stores_only_its_hash(
    db: FakePool, runner: CliRunner
) -> None:
    """With no `--password` this is the ONE moment the secret exists in readable form — it is
    never recoverable afterwards, so it must appear, and appear exactly once (a second copy
    in a scrollback or a CI log is a second place to leak it). What lands in the column must
    be an argon2id hash that verifies it, never the password itself: `create-user` is the
    only writer of `password_hash`, so a passthrough here would be undetectable at login.
    """
    result = _invoke(runner, ["create-user", "anton"])
    assert result.exit_code == 0

    printed = [line for line in result.output.splitlines() if line.startswith("password: ")]
    assert len(printed) == 1
    assert printed[0].endswith("(shown once)")
    secret = printed[0].removeprefix("password: ").removesuffix("   (shown once)")
    assert len(secret) >= 20  # secrets.token_urlsafe(18) → 24 url-safe characters
    assert result.output.count(secret) == 1

    row = db.user("anton")
    assert row is not None
    stored = row["password_hash"]
    assert stored.startswith("$argon2id$")
    assert secret not in stored
    assert verify_password(stored, secret)
    assert not verify_password(stored, secret + "x")


def test_each_generated_password_is_different(db: FakePool, runner: CliRunner) -> None:
    """A per-process or per-install constant would make every account created on the box share
    one password, and nothing in the output would look any different."""
    secrets_seen = set()
    for name in ("a", "b", "c"):
        output = _invoke(runner, ["create-user", name]).output
        line = next(line for line in output.splitlines() if line.startswith("password: "))
        secrets_seen.add(line.removeprefix("password: ").removesuffix("   (shown once)"))
    assert len(secrets_seen) == 3


def test_create_user_with_an_explicit_password_prints_no_secret(
    db: FakePool, runner: CliRunner
) -> None:
    """The deployer typed it; echoing it back only puts it in one more log."""
    result = _invoke(runner, ["create-user", "anton", "--password", "correct horse"])

    assert result.exit_code == 0
    assert "shown once" not in result.output
    assert "correct horse" not in result.output
    row = db.user("anton")
    assert row is not None
    assert verify_password(row["password_hash"], "correct horse")


def test_create_user_defaults_the_display_name_to_the_username(
    db: FakePool, runner: CliRunner
) -> None:
    """`display_name` is NOT NULL with a '' default in the schema; an empty one would render
    as a blank author in the admin panel for every account created without the option."""
    _invoke(runner, ["create-user", "anton", "--password", "x"])
    _invoke(runner, ["create-user", "olena", "--password", "x", "--display-name", "Олена К."])

    anton, olena = db.user("anton"), db.user("olena")
    assert anton is not None and olena is not None
    assert anton["display_name"] == "anton"
    assert olena["display_name"] == "Олена К."


def test_create_user_rejects_a_duplicate_username_with_a_non_zero_exit(
    db: FakePool, runner: CliRunner
) -> None:
    """A re-run of a provisioning script must fail loudly, not crash with a raw asyncpg
    traceback and not pretend to have created a second account. Exit code 1 is what the
    script branches on."""
    assert _invoke(runner, ["create-user", "anton", "--password", "first"]).exit_code == 0
    original = db.user("anton")
    assert original is not None

    result = _invoke(runner, ["create-user", "anton", "--password", "second"])

    assert result.exit_code == 1
    assert "already exists" in result.stderr
    assert "anton" in result.stderr
    assert db.usernames() == ["anton"]
    # …and the collision did not quietly overwrite the first account's password.
    assert db.user("anton") == original


def test_create_user_rejects_a_duplicate_that_differs_only_in_case(
    db: FakePool, runner: CliRunner
) -> None:
    """Usernames are CITEXT, so 'Anton' and 'anton' are the same account. If the CLI were the
    thing enforcing uniqueness instead of the schema, this would create a second row that can
    never log in — the login lookup is case-insensitive and would find the first one."""
    assert _invoke(runner, ["create-user", "anton", "--password", "x"]).exit_code == 0

    result = _invoke(runner, ["create-user", "ANTON", "--password", "x"])

    assert result.exit_code == 1
    assert "already exists" in result.stderr
    assert db.usernames() == ["anton"]


def test_create_user_trims_surrounding_whitespace(db: FakePool, runner: CliRunner) -> None:
    """A trailing space from a copy-pasted provisioning command would otherwise become part of
    the username, and nobody can type it at the login form."""
    assert _invoke(runner, ["create-user", "  anton\t", "--password", "x"]).exit_code == 0
    assert db.usernames() == ["anton"]


def test_create_user_refuses_an_empty_username_before_touching_the_database(
    db: FakePool, runner: CliRunner
) -> None:
    """An all-whitespace username is caught in the command, not by the schema: `username` is
    only NOT NULL, so '' would insert happily and produce an account that cannot be logged
    into or referred to. The check must run before the pool is opened."""
    result = _invoke(runner, ["create-user", "   ", "--password", "x"])

    assert result.exit_code == 1
    assert "username must not be empty" in result.stderr
    assert db.count("app_user") == 0
    assert db.opened == 0


# ------------------------------------------------------------------------------------
# grant-superuser / revoke-superuser
# ------------------------------------------------------------------------------------


def test_grant_superuser_promotes_a_human_without_logging_them_out(
    db: FakePool, runner: CliRunner
) -> None:
    """Granting admin must not bump `session_epoch`. It is a deliberate decision, documented
    in `_set_superuser`: `require_superuser` re-reads the row on every request, so the panel
    appears on the next click, and kicking the person out of the chat they are in the middle
    of would be a side effect nobody asked for."""
    db.add_user("anton", session_epoch=7)

    result = _invoke(runner, ["grant-superuser", "anton"])

    assert result.exit_code == 0
    row = db.user("anton")
    assert row is not None
    assert row["is_superuser"] == 1
    assert row["session_epoch"] == 7
    assert "'anton' is now a superuser" in result.output


def test_revoke_superuser_demotes_a_human_without_logging_them_out(
    db: FakePool, runner: CliRunner
) -> None:
    """The mirror image, and the one that matters under pressure: revoking has to take effect
    on the next request, which is what makes NOT bumping the epoch safe rather than lax."""
    db.add_user("anton", is_superuser=True, session_epoch=7)

    result = _invoke(runner, ["revoke-superuser", "anton"])

    assert result.exit_code == 0
    row = db.user("anton")
    assert row is not None
    assert row["is_superuser"] == 0
    assert row["session_epoch"] == 7
    assert "'anton' is no longer a superuser" in result.output


def test_grant_superuser_accepts_the_username_in_any_case(db: FakePool, runner: CliRunner) -> None:
    """The lookup leans on CITEXT rather than lower()-ing the argument. A deployer who types
    the name with the capital it has on their screen must not get 'no such user'."""
    db.add_user("anton")

    result = _invoke(runner, ["grant-superuser", "AnToN"])

    assert result.exit_code == 0
    row = db.user("anton")
    assert row is not None
    assert row["is_superuser"] == 1
    # The message echoes the STORED spelling, not what was typed, so it confirms the row hit.
    assert "'anton' is now a superuser" in result.output


@pytest.mark.parametrize("command", ["grant-superuser", "revoke-superuser"])
def test_superuser_commands_refuse_a_guest(db: FakePool, runner: CliRunner, command: str) -> None:
    """A guest is an anonymous browser session with an unusable password hash and no login at
    all. Promoting one would hand the admin panel to whoever holds that cookie — and to anyone
    who gets it — so `require_superuser` refuses guests on top of the flag, and the flag must
    never be set in the first place. Exit code 1: a script that promotes the wrong id must
    stop, not continue."""
    db.add_user("guest_2f9c", kind="guest")

    result = _invoke(runner, [command, "guest_2f9c"])

    assert result.exit_code == 1
    assert "guests can never be superusers" in result.stderr
    assert "guest_2f9c" in result.stderr
    row = db.user("guest_2f9c")
    assert row is not None
    assert row["is_superuser"] == 0


@pytest.mark.parametrize(
    "args",
    [
        ["grant-superuser", "ghost"],
        ["revoke-superuser", "ghost"],
        ["set-password", "ghost", "--password", "x"],
        ["disable-user", "ghost"],
    ],
    ids=["grant", "revoke", "set-password", "disable"],
)
def test_a_command_against_a_missing_user_exits_non_zero_and_names_it(
    db: FakePool, runner: CliRunner, args: list[str]
) -> None:
    """Typoing the username is the most likely mistake at a deploy prompt. Every one of these
    must fail the process AND print which name it looked for — 'no such user' without the name
    is useless when the command is being run from a script with a variable in it."""
    db.add_user("anton", session_epoch=3)

    result = _invoke(runner, args)

    assert result.exit_code == 1
    assert "no such user: 'ghost'" in result.stderr
    # Nothing was done to the account that DOES exist.
    row = db.user("anton")
    assert row is not None
    assert row["session_epoch"] == 3
    assert row["is_active"] == 1
    assert row["is_superuser"] == 0


# ------------------------------------------------------------------------------------
# set-password / disable-user — the two commands that revoke live sessions
# ------------------------------------------------------------------------------------


def test_set_password_replaces_the_hash_and_bumps_the_session_epoch(
    db: FakePool, runner: CliRunner
) -> None:
    """The epoch bump is the whole point. Sessions are stateless signed cookies carrying the
    epoch they were minted at, so without the increment a password reset after a compromise
    leaves every stolen cookie working indefinitely — the new password would protect only the
    login form, which the attacker no longer needs."""
    _invoke(runner, ["create-user", "anton", "--password", "old-secret"])
    before = db.user("anton")
    assert before is not None

    result = _invoke(runner, ["set-password", "anton", "--password", "new-secret"])

    assert result.exit_code == 0
    after = db.user("anton")
    assert after is not None
    assert after["session_epoch"] == before["session_epoch"] + 1
    assert verify_password(after["password_hash"], "new-secret")
    assert not verify_password(after["password_hash"], "old-secret")
    assert "all sessions invalidated" in result.output


def test_set_password_prompts_with_confirmation_when_the_option_is_omitted(
    db: FakePool, runner: CliRunner
) -> None:
    """The intended interactive form: the secret is typed, hidden, twice. Typing it once as an
    argument would leave it in the shell history of the production host."""
    db.add_user("anton")

    result = _invoke(runner, ["set-password", "anton"], input="typed-secret\ntyped-secret\n")

    assert result.exit_code == 0
    row = db.user("anton")
    assert row is not None
    assert verify_password(row["password_hash"], "typed-secret")
    assert "typed-secret" not in result.output  # hide_input=True


def test_set_password_does_not_accept_a_mistyped_confirmation(
    db: FakePool, runner: CliRunner
) -> None:
    """A typo in a password nobody can read back locks the account out of its own deployment.
    The confirmation prompt exists to catch exactly that, and must re-ask rather than accept
    the first answer."""
    db.add_user("anton", password_hash="$argon2id$untouched")

    result = _invoke(runner, ["set-password", "anton"], input="one\ntwo\n")

    assert result.exit_code != 0
    row = db.user("anton")
    assert row is not None
    assert row["password_hash"] == "$argon2id$untouched"


def test_set_password_touches_only_that_account(db: FakePool, runner: CliRunner) -> None:
    """The UPDATE is keyed on the id `_require_user` resolved. Losing that predicate would
    reset every password in the deployment, and the success message would look identical."""
    db.add_user("anton", password_hash="$argon2id$anton", session_epoch=1)
    db.add_user("olena", password_hash="$argon2id$olena", session_epoch=1)

    assert _invoke(runner, ["set-password", "anton", "--password", "new"]).exit_code == 0

    olena = db.user("olena")
    assert olena is not None
    assert olena["password_hash"] == "$argon2id$olena"
    assert olena["session_epoch"] == 1


def test_disable_user_deactivates_and_bumps_the_session_epoch(
    db: FakePool, runner: CliRunner
) -> None:
    """`is_active = FALSE` alone is advisory: it stops the next LOGIN, not the session already
    in the browser. The epoch bump is what turns 'disabled' into enforced, and it is the only
    lever there is for removing someone's access right now."""
    db.add_user("anton", session_epoch=4)

    result = _invoke(runner, ["disable-user", "anton"])

    assert result.exit_code == 0
    row = db.user("anton")
    assert row is not None
    assert row["is_active"] == 0
    assert row["session_epoch"] == 5
    assert "disabled 'anton'; all sessions invalidated" in result.output


def test_disable_user_touches_only_that_account(db: FakePool, runner: CliRunner) -> None:
    """The failure this prevents is the worst outage the CLI can cause: an unscoped UPDATE
    that disables and logs out every account in the deployment at once."""
    db.add_user("anton", session_epoch=1)
    db.add_user("olena", session_epoch=1)

    assert _invoke(runner, ["disable-user", "anton"]).exit_code == 0

    olena = db.user("olena")
    assert olena is not None
    assert olena["is_active"] == 1
    assert olena["session_epoch"] == 1


def test_disable_user_is_idempotent(db: FakePool, runner: CliRunner) -> None:
    """Running it twice is what a nervous operator does. It must not fail the second time —
    and each run genuinely re-revokes, which is the useful behaviour if a session was minted
    in between."""
    db.add_user("anton")

    assert _invoke(runner, ["disable-user", "anton"]).exit_code == 0
    assert _invoke(runner, ["disable-user", "anton"]).exit_code == 0

    row = db.user("anton")
    assert row is not None
    assert row["is_active"] == 0
    assert row["session_epoch"] == 2


# ------------------------------------------------------------------------------------
# purge-guests — the only command in the codebase that deletes anything
# ------------------------------------------------------------------------------------


def test_purge_guests_deletes_stale_guests_and_leaves_humans_alone(
    db: FakePool, runner: CliRunner
) -> None:
    """The one destructive command, and the one whose blast radius is bounded by a single
    predicate. A human account matching the age window — every account is older than 30 days
    eventually — must never be inside it, however long the deployment has been running."""
    db.add_user("anton", last_seen_days_ago=400)
    db.add_user("root", is_superuser=True, last_seen_days_ago=400)
    db.add_user("guest_old", kind="guest", last_seen_days_ago=90)
    db.add_user("guest_today", kind="guest", last_seen_days_ago=0.1)

    result = _invoke(runner, ["purge-guests", "--yes"])

    assert result.exit_code == 0
    assert db.usernames() == ["anton", "root", "guest_today"]
    assert "purged 1 guest account(s)" in result.output


def test_purge_guests_respects_the_age_window(db: FakePool, runner: CliRunner) -> None:
    """`--older-than-days` is the operator's only dial, and `last_seen_at` is refreshed at most
    once a day, so a guest who is chatting right now must be out of reach of a small window.
    Off-by-one here either deletes an active visitor's conversation mid-turn or never reclaims
    anything."""
    db.add_user("guest_ancient", kind="guest", last_seen_days_ago=100)
    db.add_user("guest_recent", kind="guest", last_seen_days_ago=10)
    db.add_user("guest_now", kind="guest", last_seen_days_ago=0.01)

    assert _invoke(runner, ["purge-guests", "--older-than-days", "30", "--yes"]).exit_code == 0
    assert db.usernames() == ["guest_recent", "guest_now"]

    assert _invoke(runner, ["purge-guests", "--older-than-days", "5", "--yes"]).exit_code == 0
    assert db.usernames() == ["guest_now"]


def test_purge_guests_falls_back_to_created_at_for_a_guest_never_seen_again(
    db: FakePool, runner: CliRunner
) -> None:
    """`last_seen_at` is NULL until the sliding refresh first fires, so a crawler that loaded
    one page and left has only `created_at`. Without the coalesce those rows are unreachable
    forever and the table grows without bound — which is the exact housekeeping this command
    exists to do."""
    db.add_user("guest_drive_by", kind="guest", created_days_ago=120, last_seen_days_ago=None)
    db.add_user("guest_arrived_today", kind="guest", created_days_ago=0, last_seen_days_ago=None)

    assert _invoke(runner, ["purge-guests", "--yes"]).exit_code == 0
    assert db.usernames() == ["guest_arrived_today"]


def test_purge_guests_zero_days_takes_every_guest_and_says_so(
    db: FakePool, runner: CliRunner
) -> None:
    """0 is documented as 'every guest'. The prompt has to say that in words: '0 guest
    account(s)' would read like a no-op right before the most destructive run there is."""
    db.add_user("anton")
    db.add_user("guest_a", kind="guest", last_seen_days_ago=0.01)
    db.add_user("guest_b", kind="guest", last_seen_days_ago=90)

    result = _invoke(runner, ["purge-guests", "--older-than-days", "0"], input="y\n")

    assert result.exit_code == 0
    assert "delete every guest account and all of their threads and classifications?" in (
        result.output
    )
    assert db.usernames() == ["anton"]


def test_purge_guests_aborts_and_exits_non_zero_when_the_confirmation_is_declined(
    db: FakePool, runner: CliRunner
) -> None:
    """Answering 'n' must delete nothing AND fail the process. If an aborted purge exited 0,
    a `make purge && make report` chain would report success on a purge that never happened —
    and the confirmation would be theatre."""
    db.add_user("guest_old", kind="guest", last_seen_days_ago=90)

    result = _invoke(runner, ["purge-guests"], input="n\n")

    assert result.exit_code == 1
    assert "aborted" in result.output
    assert db.usernames() == ["guest_old"]
    assert not any(statement.lstrip().startswith("DELETE") for statement in db.statements)


def test_purge_guests_deletes_when_the_confirmation_is_accepted(
    db: FakePool, runner: CliRunner
) -> None:
    """The other half: the prompt must be answerable, and it must name the number of accounts
    at risk so the operator can notice a window they did not mean to type."""
    db.add_user("guest_old", kind="guest", last_seen_days_ago=90)
    db.add_user("guest_older", kind="guest", last_seen_days_ago=91)

    result = _invoke(runner, ["purge-guests"], input="y\n")

    assert result.exit_code == 0
    assert "delete 2 guest account(s)" in result.output
    assert db.usernames() == []


def test_purge_guests_skips_the_prompt_entirely_when_nothing_matches(
    db: FakePool, runner: CliRunner
) -> None:
    """Run from cron or a Makefile with no `--yes`, an empty purge must not block on a prompt
    with no tty behind it. It reports and exits 0, having issued no DELETE at all."""
    db.add_user("anton")
    db.add_user("guest_today", kind="guest", last_seen_days_ago=0.01)

    result = _invoke(runner, ["purge-guests"])  # no input available: a prompt would fail here

    assert result.exit_code == 0
    assert "no guests older than 30 day(s)" in result.output
    assert not any(statement.lstrip().startswith("DELETE") for statement in db.statements)
    assert db.usernames() == ["anton", "guest_today"]


def test_purge_guests_takes_the_guests_threads_and_classifications_with_them(
    db: FakePool, runner: CliRunner
) -> None:
    """The safety argument for one `DELETE FROM app_user` is the cascade declared in
    migrations 0002 and 0004. If a foreign key were ever added without ON DELETE CASCADE, the
    delete would start failing on a constraint mid-purge — or, worse, orphan rows that still
    hold the visitor's product descriptions after their account is gone."""
    guest = db.add_user("guest_old", kind="guest", last_seen_days_ago=90)
    human = db.add_user("anton", last_seen_days_ago=90)
    db.add_thread("thr_guest", guest, items=3)
    db.add_classification("cls_guest", guest)
    db.add_thread("thr_anton", human, items=2)
    db.add_classification("cls_anton", human)

    assert _invoke(runner, ["purge-guests", "--yes"]).exit_code == 0

    assert db.usernames() == ["anton"]
    assert db.count("chat_thread") == 1
    assert db.count("chat_thread_item") == 2
    assert db.count("classification") == 1
    assert db.count("classification_code") == 1  # the per-code rows cascade off classification


def test_purge_guests_counts_thousands_readably(db: FakePool, runner: CliRunner) -> None:
    """After a public demo the number is four or five digits, and it is the number the operator
    reads before typing 'y'. '1234' and '12340' are one glance apart; '1,234' is not."""
    db.add_guests(1234, days_ago=90)

    result = _invoke(runner, ["purge-guests"], input="y\n")

    assert result.exit_code == 0
    assert "delete 1,234 guest account(s)" in result.output
    assert "purged 1,234 guest account(s)" in result.output
    assert db.count("app_user") == 0


def test_purge_guests_rejects_a_negative_window_before_opening_the_pool(
    db: FakePool, runner: CliRunner
) -> None:
    """`min=0` on the option. A negative window would make `now() - make_interval(...)` a time
    in the FUTURE, which matches every guest — the most destructive possible reading of an
    argument that was obviously a typo."""
    db.add_user("guest_today", kind="guest", last_seen_days_ago=0.01)

    result = _invoke(runner, ["purge-guests", "--older-than-days", "-1", "--yes"])

    assert result.exit_code == 2
    assert db.usernames() == ["guest_today"]
    assert db.opened == 0


# ------------------------------------------------------------------------------------
# list-users
# ------------------------------------------------------------------------------------


def test_list_users_hides_guests_and_reports_them_as_a_total(
    db: FakePool, runner: CliRunner
) -> None:
    """In public mode guests outnumber humans by whatever the demo's traffic turns out to be.
    A listing that scrolls the handful of managed accounts off the screen is not a listing of
    the accounts anyone manages — but silently omitting them would hide the reason to purge,
    so they are still counted."""
    db.add_user("anton", last_seen_days_ago=1)
    db.add_user("guest_2f9c", kind="guest", last_seen_days_ago=1)
    db.add_user("guest_8a01", kind="guest", last_seen_days_ago=1)

    result = _invoke(runner, ["list-users"])

    assert result.exit_code == 0
    assert "anton" in result.output
    assert "guest_2f9c" not in result.output
    assert "guest_8a01" not in result.output
    assert "2 guest account(s) hidden — show them with --guests" in result.output


def test_list_users_shows_guests_with_the_flag(db: FakePool, runner: CliRunner) -> None:
    """The flag has to actually widen the WHERE clause — it is passed into the query as a
    parameter rather than switching SQL, which is exactly the shape that silently does
    nothing if the cast or the predicate is wrong."""
    db.add_user("anton", last_seen_days_ago=1)
    db.add_user("guest_2f9c", kind="guest", last_seen_days_ago=1)

    result = _invoke(runner, ["list-users", "--guests"])

    assert result.exit_code == 0
    assert "anton" in result.output
    assert "guest_2f9c" in result.output
    assert "hidden" not in result.output


def test_list_users_marks_admins_and_disabled_accounts(db: FakePool, runner: CliRunner) -> None:
    """This listing is how a deployer answers 'who can reach the admin panel' and 'did the
    disable actually land'. Both answers are a single column, and both default to the quiet
    value, so a flag that never reaches the output reads as 'no admins'."""
    db.add_user("root", is_superuser=True, last_seen_days_ago=1)
    db.add_user("anton", last_seen_days_ago=1)
    db.add_user("olena", is_active=False, last_seen_days_ago=1)

    lines = {
        line.split()[1]: line for line in _invoke(runner, ["list-users"]).output.splitlines()[2:]
    }

    assert "ADMIN" in lines["root"]
    assert "ADMIN" not in lines["anton"]
    assert "DISABLED" in lines["olena"]
    assert "active" in lines["anton"]


def test_list_users_shows_the_session_epoch_and_thread_count(
    db: FakePool, runner: CliRunner
) -> None:
    """The epoch is the number that tells an operator whether the revoke they just ran took
    effect, and the thread count is what makes 'this guest has 40 threads' visible before a
    purge. The count comes from a LEFT JOIN, so an account with no threads must still list —
    an inner join would drop every fresh account from the listing."""
    anton = db.add_user("anton", session_epoch=3, last_seen_days_ago=1)
    db.add_user("olena", last_seen_days_ago=1)
    db.add_thread("thr_1", anton)
    db.add_thread("thr_2", anton)

    lines = {
        line.split()[1]: line for line in _invoke(runner, ["list-users"]).output.splitlines()[2:]
    }

    # id  username  kind  role  state  ep  threads  last-active
    assert lines["anton"].split()[5:7] == ["3", "2"]
    assert lines["olena"].split()[5:7] == ["0", "0"]


def test_list_users_falls_back_to_last_login_and_then_to_never(
    db: FakePool, runner: CliRunner
) -> None:
    """A guest never logs in, so `last_login_at` is always NULL for one; a human created a
    minute ago has neither. `.strftime()` on a NULL is an AttributeError, and this command is
    the first thing anybody runs after creating the first account."""
    db.add_user("never_seen")
    db.add_user("logged_in_only", last_login_days_ago=2)
    db.add_user("seen", last_seen_days_ago=1, last_login_days_ago=30)

    lines = {
        line.split()[1]: line
        for line in _invoke(runner, ["list-users"]).output.splitlines()[2:]
        if line.strip()
    }

    assert lines["never_seen"].endswith("never")
    assert lines["logged_in_only"].endswith(_stamp(db, "logged_in_only", "last_login_at"))
    # last_seen_at wins: it is the column that means the same thing for a guest and a human.
    assert lines["seen"].endswith(_stamp(db, "seen", "last_seen_at"))
    assert not lines["seen"].endswith(_stamp(db, "seen", "last_login_at"))


def test_list_users_on_an_empty_database_says_how_to_create_the_first_account(
    db: FakePool, runner: CliRunner
) -> None:
    """The very first command run against a fresh deployment. An empty table is the expected
    state, not an error, and the answer to 'now what' has to be in the output — there is no
    registration page to fall back on."""
    result = _invoke(runner, ["list-users"])

    assert result.exit_code == 0
    assert "no users yet" in result.output
    assert "python -m app.cli create-user <username>" in result.output


def test_list_users_with_only_guests_still_mentions_them(db: FakePool, runner: CliRunner) -> None:
    """A public-mode deployment with no human accounts yet: 'no users yet' on its own would be
    a lie about a table with a thousand rows in it, and would hide the only thing worth doing
    (purging them)."""
    db.add_guests(1234, days_ago=1)

    result = _invoke(runner, ["list-users"])

    assert result.exit_code == 0
    assert "no users yet" in result.output
    assert "(1,234 guest account(s); show them with --guests)" in result.output


# ------------------------------------------------------------------------------------
# ingest
# ------------------------------------------------------------------------------------


def _stub_ingest(
    monkeypatch: pytest.MonkeyPatch, *, result: IngestResult | None = None, error: Exception | None
) -> dict[str, Any]:
    """Replace `ingest_file` (covered end to end by tests/test_ingest_invariants.py) so that
    what is under test here is the CLI's half: the connection it borrows, the flags it passes
    on, the exit code and the report."""
    seen: dict[str, Any] = {}

    async def _fake(conn: Any, path: Path, *, activate: bool = True) -> IngestResult:
        seen["conn"] = conn
        seen["path"] = path
        seen["activate"] = activate
        if error is not None:
            raise error
        assert result is not None
        return result

    monkeypatch.setattr(cli_mod, "ingest_file", _fake)
    return seen


def test_ingest_reports_the_dataset_it_loaded_and_activated(
    db: FakePool, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The counts and the sha are the operator's only evidence that the container is serving
    the tariff they think it is — `/readyz` compares against that same digest."""
    _stub_ingest(
        monkeypatch,
        result=IngestResult(
            dataset_id=7,
            sha256="5a113fc0",
            node_count=14187,
            terminal_count=10490,
            created=True,
            activated=True,
        ),
        error=None,
    )

    result = _invoke(runner, ["ingest", "data/uktzed_hierarchical.json"])

    assert result.exit_code == 0
    assert "dataset 7 ingested: 14,187 nodes, 10,490 terminals, sha256 5a113fc0" in result.output
    assert "activated" in result.output


def test_ingest_of_an_already_present_dataset_is_a_success_not_a_load(
    db: FakePool, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ingest is idempotent per content sha256 and runs on every container start in some
    setups. Re-running it must exit 0 and must NOT claim to have loaded anything — and with
    nothing activated, it must not claim that either."""
    _stub_ingest(
        monkeypatch,
        result=IngestResult(
            dataset_id=7,
            sha256="5a113fc0",
            node_count=14187,
            terminal_count=10490,
            created=False,
            activated=False,
        ),
        error=None,
    )

    result = _invoke(runner, ["ingest", "data/uktzed_hierarchical.json"])

    assert result.exit_code == 0
    assert "dataset 7 already present" in result.output
    assert "ingested" not in result.output
    assert "activated" not in result.output


def test_ingest_defaults_to_the_committed_dataset_and_activates(
    db: FakePool, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`make ingest`, the Dockerfile and the README all call `python -m app.cli ingest` with no
    path. Changing the default silently makes those three ingest nothing."""
    seen = _stub_ingest(
        monkeypatch,
        result=IngestResult(
            dataset_id=1, sha256="abc", node_count=1, terminal_count=1, created=True, activated=True
        ),
        error=None,
    )

    assert _invoke(runner, ["ingest"]).exit_code == 0

    assert seen["path"] == Path("data/uktzed_hierarchical.json")
    assert seen["activate"] is True
    # ingest_file needs a Connection, not the Pool: copy_records_to_table is connection-level.
    assert seen["conn"] is db


def test_ingest_no_activate_loads_without_switching_the_served_dataset(
    db: FakePool, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Staging a new tariff without flipping the running app onto it is the whole reason the
    flag exists; if it did not reach `ingest_file`, the swap would happen mid-demo."""
    seen = _stub_ingest(
        monkeypatch,
        result=IngestResult(
            dataset_id=2,
            sha256="abc",
            node_count=1,
            terminal_count=1,
            created=True,
            activated=False,
        ),
        error=None,
    )

    result = _invoke(runner, ["ingest", "data/new.json", "--no-activate"])

    assert result.exit_code == 0
    assert seen["activate"] is False
    assert seen["path"] == Path("data/new.json")
    assert "activated" not in result.output


def test_ingest_refusal_exits_non_zero_and_prints_every_failure(
    db: FakePool, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tariff update that trips the invariants must stop the deploy, and must list ALL of
    what changed rather than the first line — that list is the reviewer's diff. repr()-ing the
    exception would collapse it onto one line with escaped newlines."""
    _stub_ingest(
        monkeypatch,
        error=IngestInvariantError(
            [
                "node_count: expected 14,187, got 12,004",
                "terminal_count: expected 10,490, got 9,001",
            ]
        ),
    )

    result = _invoke(runner, ["ingest", "data/tariff_2027.json"])

    assert result.exit_code == 1
    assert "ingest refused the file:" in result.stderr
    assert "node_count: expected 14,187, got 12,004" in result.stderr
    assert "terminal_count: expected 10,490, got 9,001" in result.stderr


# ------------------------------------------------------------------------------------
# The shape of the CLI itself
# ------------------------------------------------------------------------------------


def test_the_cli_exposes_exactly_the_commands_the_deploy_docs_invoke(runner: CliRunner) -> None:
    """These eight names are hard-coded in the module docstring, the README, the Makefile and
    the Dockerfile. Renaming one is a documentation change everywhere or a broken deploy."""
    assert {command.name for command in cli_app.registered_commands} == {
        "create-user",
        "grant-superuser",
        "revoke-superuser",
        "set-password",
        "disable-user",
        "purge-guests",
        "ingest",
        "list-users",
    }


def test_the_bare_cli_prints_help_and_exits_non_zero(runner: CliRunner) -> None:
    """`no_args_is_help=True`. A bare invocation is a mistake, and a mistake that exits 0 in a
    deploy script looks like the command ran."""
    result = runner.invoke(cli_app, [])

    assert result.exit_code == 2
    assert "create-user" in result.output


def test_every_command_closes_its_pool_even_when_it_fails(db: FakePool, runner: CliRunner) -> None:
    """`_run` opens a pool per command and closes it in a `finally`. The failure paths are the
    ones that matter — an exception escaping before the close leaves asyncio complaining about
    a pending connection on the way out, on top of whatever actually went wrong."""
    db.add_user("guest_2f9c", kind="guest")

    assert _invoke(runner, ["grant-superuser", "ghost"]).exit_code == 1
    assert _invoke(runner, ["grant-superuser", "guest_2f9c"]).exit_code == 1
    assert _invoke(runner, ["create-user", "anton", "--password", "x"]).exit_code == 0
    assert _invoke(runner, ["create-user", "anton", "--password", "x"]).exit_code == 1

    assert db.opened == 4
    assert db.closed == 4  # the fixture asserts the balance again after every test
