"""Guests are real users, the mode switch is instant, and the admin panel is invisible.

Three claims are load-bearing enough that the demo fails badly if any of them is wrong, and
each one is asserted here against real SQL (the `FakePool` technique from
`tests/test_store_isolation.py` — SQLite executing the app's own statements, so deleting a
predicate makes a test fail rather than a double agree with itself):

  * **A guest persists.** The cookie minted on the first visit resolves to the SAME
    `app_user.id` on every later request, which is the whole reason "my history is still
    there after I close the browser" is true.
  * **The switch is instant.** The moment `access_mode` becomes 'private', the very next
    request from an already-open guest tab is a 401 with its session cleared. No TTL, no
    "within a few seconds", no worker that has not noticed yet.
  * **The panel is hidden.** `require_superuser` answers 404 — never 403 — to a normal user,
    to a guest and to an anonymous prober, and does not create an `app_user` row while doing
    so, because a probe that mints a row is a hidden panel you can fill a table through.

And one property that is cheap to assert and expensive to discover in production: the guest
password sentinel cannot be verified against ANY input, so a guest row is not a login
waiting to be guessed.
"""

from __future__ import annotations

import re
import sqlite3
import time
from collections.abc import Iterator
from typing import Any, cast

import asyncpg
import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app import db as db_module
from app import runtime_settings as rs
from app.auth import guest as guest_module
from app.auth.deps import current_user, require_superuser, resolve_session
from app.auth.guest import GUEST_PASSWORD_SENTINEL, ensure_guest
from app.auth.passwords import hash_password, verify_password

# Mirrors app_user after migration 0005, plus app_setting. CITEXT becomes COLLATE NOCASE,
# which gives SQLite the same case-insensitive uniqueness the real column has — so the
# collision-retry path below is exercised against the same rule Postgres enforces.
_SCHEMA = """
CREATE TABLE app_user (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT    NOT NULL,
    display_name  TEXT    NOT NULL DEFAULT '',
    is_active     INTEGER NOT NULL DEFAULT 1,
    is_reviewer   INTEGER NOT NULL DEFAULT 0,
    session_epoch INTEGER NOT NULL DEFAULT 0,
    kind          TEXT    NOT NULL DEFAULT 'human' CHECK (kind IN ('human', 'guest')),
    is_superuser  INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_login_at TEXT,
    last_seen_at  TEXT
);

CREATE TABLE app_setting (
    key        TEXT    PRIMARY KEY,
    value      TEXT    NOT NULL,
    updated_at TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_by INTEGER REFERENCES app_user(id) ON DELETE SET NULL
);
"""

_PARAM = re.compile(r"\$(\d+)")
_DAY = 86_400


def _translate(sql: str, args: tuple[Any, ...]) -> tuple[str, list[Any]]:
    collected: list[Any] = []

    def _sub(match: re.Match[str]) -> str:
        collected.append(args[int(match.group(1)) - 1])
        return "?"

    sql = _PARAM.sub(_sub, sql)
    return sql.replace("::jsonb", "").replace("now()", "CURRENT_TIMESTAMP"), collected


class FakePool:
    def __init__(self) -> None:
        self._conn = sqlite3.connect(":memory:")
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self.statements: list[str] = []

    def _run(self, sql: str, args: tuple[Any, ...]) -> list[sqlite3.Row]:
        self.statements.append(" ".join(sql.split()))
        translated, params = _translate(sql, args)
        try:
            cursor = self._conn.execute(translated, params)
        except sqlite3.IntegrityError as exc:
            self._conn.rollback()
            if "UNIQUE" in str(exc):
                # asyncpg's spelling, so the app's `except asyncpg.UniqueViolationError`
                # retry is the branch actually under test.
                raise asyncpg.UniqueViolationError(str(exc)) from exc
            raise
        rows = cursor.fetchall()
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

    # -- test helpers -------------------------------------------------------------------
    def set_mode(self, mode: str) -> None:
        self._conn.execute(
            "INSERT INTO app_setting (key, value) VALUES ('access_mode', ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (f'"{mode}"',),
        )
        self._conn.commit()

    def add_human(
        self, username: str, *, superuser: bool = False, active: bool = True, epoch: int = 0
    ) -> int:
        cur = self._conn.execute(
            "INSERT INTO app_user (username, password_hash, display_name, kind, is_active, "
            "is_superuser, session_epoch) VALUES (?, ?, ?, 'human', ?, ?, ?) RETURNING id",
            (username, hash_password("correct horse"), username, active, superuser, epoch),
        )
        user_id = int(cur.fetchone()["id"])
        self._conn.commit()
        return user_id

    def users(self) -> list[sqlite3.Row]:
        return self._conn.execute("SELECT * FROM app_user ORDER BY id").fetchall()

    def user_count(self, kind: str | None = None) -> int:
        sql = "SELECT count(*) AS n FROM app_user"
        params: tuple[Any, ...] = ()
        if kind is not None:
            sql += " WHERE kind = ?"
            params = (kind,)
        return int(self._conn.execute(sql, params).fetchone()["n"])

    def count_statements(self, needle: str) -> int:
        return sum(1 for s in self.statements if needle in s)


@pytest.fixture
def pool(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakePool]:
    fake = FakePool()
    monkeypatch.setattr(db_module, "_pool", cast(asyncpg.Pool, fake))
    rs.invalidate_cache()
    yield fake
    rs.invalidate_cache()


def make_request(session: dict[str, Any] | None = None) -> Request:
    """A Request with a session dict, which is all `current_user` and `ensure_guest` touch.

    Starlette's SessionMiddleware puts the decoded cookie in `scope["session"]` and re-signs
    it on the way out if it was mutated; passing the dict in and reading it back out is
    therefore an honest stand-in for "the browser sends the cookie and gets a new one".
    """
    scope: dict[str, Any] = {
        "type": "http",
        "method": "GET",
        "path": "/api/me",
        "headers": [],
        "session": {} if session is None else session,
    }
    return Request(scope)


def cookie_of(request: Request) -> dict[str, Any]:
    """What the browser would store and send back next time — a COPY, so that mutating the
    next request's session cannot reach backwards into this one."""
    return dict(request.session)


# --------------------------------------------------------------------------------------
# The sentinel: a guest row is not a login
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "candidate",
    [
        "",
        " ",
        GUEST_PASSWORD_SENTINEL,
        GUEST_PASSWORD_SENTINEL.strip("!"),
        "!",
        "guest",
        "Гість",
        "correct horse",
        "\x00",
        "a" * 1024,
    ],
)
def test_the_guest_sentinel_never_verifies(candidate: str) -> None:
    assert verify_password(GUEST_PASSWORD_SENTINEL, candidate) is False


def test_the_sentinel_is_not_an_argon2_hash_and_is_not_empty() -> None:
    """Both halves matter.

    Not an argon2 PHC string, so `verify_password` can only reach its InvalidHashError
    branch — there is no input for which the verifier even gets as far as comparing. And not
    the empty string, because `''` is what a missing column default or a half-written INSERT
    produces by accident, and a sentinel has to be distinguishable from a mistake.
    """
    assert GUEST_PASSWORD_SENTINEL
    assert not GUEST_PASSWORD_SENTINEL.startswith("$argon2")
    assert GUEST_PASSWORD_SENTINEL.startswith("!")


# --------------------------------------------------------------------------------------
# Minting, and persistence across a browser restart
# --------------------------------------------------------------------------------------


async def test_public_mode_mints_a_guest_row(pool: FakePool) -> None:
    pool.set_mode("public")

    request = make_request()
    user = await current_user(request)

    assert user.kind == "guest"
    assert user.username.startswith("guest_")
    assert user.display_name == "Гість"
    assert not user.is_superuser

    (row,) = pool.users()
    assert row["kind"] == "guest"
    assert row["is_active"] == 1
    assert row["is_superuser"] == 0
    assert row["password_hash"] == GUEST_PASSWORD_SENTINEL
    assert row["last_seen_at"] is not None

    # The session is the shape /api/login writes, and nothing more.
    assert set(request.session) == {"uid", "ep", "iat"}
    assert request.session["uid"] == user.id
    assert request.session["ep"] == row["session_epoch"]


async def test_the_same_cookie_resolves_to_the_same_guest(pool: FakePool) -> None:
    """The persistence guarantee, stated as a test.

    First visit mints. Closing the browser and coming back — a fresh Request carrying only
    what the cookie held — must land on the SAME user id, because that id is what every
    thread, every message and every classification is filed under.
    """
    pool.set_mode("public")

    first = make_request()
    minted = await current_user(first)
    cookie = cookie_of(first)

    for _ in range(3):
        later = make_request(dict(cookie))
        again = await current_user(later)
        assert again.id == minted.id
        assert again.username == minted.username
        assert again.kind == "guest"

    assert pool.user_count() == 1, "a returning visitor must not mint a second row"


async def test_two_visitors_get_two_identities(pool: FakePool) -> None:
    pool.set_mode("public")

    one = await current_user(make_request())
    two = await current_user(make_request())

    assert one.id != two.id
    assert one.username != two.username
    assert pool.user_count("guest") == 2


async def test_a_username_collision_is_retried(
    pool: FakePool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """96 bits never collides; the retry exists so that if it ever did, a visitor sees the
    app rather than a 500. Forcing the collision is the only way to know the branch works."""
    tokens = iter(["taken", "taken", "free"])
    monkeypatch.setattr(guest_module.secrets, "token_urlsafe", lambda _n: next(tokens))
    pool.set_mode("public")

    first = await ensure_guest(make_request())
    assert first.username == "guest_taken"

    second = await ensure_guest(make_request())
    assert second.username == "guest_free"
    assert pool.user_count("guest") == 2


async def test_a_dead_mint_is_a_503_not_a_500(
    pool: FakePool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(guest_module.secrets, "token_urlsafe", lambda _n: "always-the-same")
    pool.set_mode("public")

    await ensure_guest(make_request())
    with pytest.raises(HTTPException) as caught:
        await ensure_guest(make_request())
    assert caught.value.status_code == 503


# --------------------------------------------------------------------------------------
# The kill switch
# --------------------------------------------------------------------------------------


async def test_flipping_to_private_logs_a_guest_out_on_the_next_request(pool: FakePool) -> None:
    """THE test for the demo's kill switch.

    A guest is mid-conversation with a valid, unexpired, correctly-signed cookie. The author
    flips the mode. The next request that tab makes — not the next one after a cache TTL —
    is a 401, and the session is cleared so the browser stops sending the dead cookie.
    """
    pool.set_mode("public")
    first = make_request()
    guest = await current_user(first)
    cookie = cookie_of(first)

    # Still fine while the demo is open.
    assert (await current_user(make_request(dict(cookie)))).id == guest.id

    pool.set_mode("private")

    request = make_request(dict(cookie))
    with pytest.raises(HTTPException) as caught:
        await current_user(request)
    assert caught.value.status_code == 401
    assert request.session == {}, "the dead cookie must be cleared, not merely rejected"


async def test_private_mode_does_not_mint_for_an_anonymous_visitor(pool: FakePool) -> None:
    pool.set_mode("private")

    request = make_request()
    with pytest.raises(HTTPException) as caught:
        await current_user(request)
    assert caught.value.status_code == 401
    assert pool.user_count() == 0
    assert request.session == {}


async def test_the_default_mode_is_private_so_an_unconfigured_deployment_is_closed(
    pool: FakePool,
) -> None:
    """No row in app_setting at all — a freshly migrated database. Nobody gets in."""
    with pytest.raises(HTTPException) as caught:
        await current_user(make_request())
    assert caught.value.status_code == 401
    assert pool.user_count() == 0


async def test_flipping_back_to_public_hands_out_a_new_identity(pool: FakePool) -> None:
    """Documented consequence of clearing the session on the flip: the old guest row still
    exists, but the browser no longer holds a cookie pointing at it, so reopening the demo
    starts a fresh history rather than silently resurrecting the old one."""
    pool.set_mode("public")
    first = make_request()
    old = await current_user(first)
    cookie = cookie_of(first)

    pool.set_mode("private")
    rejected = make_request(dict(cookie))
    with pytest.raises(HTTPException):
        await current_user(rejected)

    pool.set_mode("public")
    new = await current_user(make_request(cookie_of(rejected)))
    assert new.id != old.id
    assert pool.user_count("guest") == 2


async def test_a_human_is_unaffected_by_either_mode(pool: FakePool) -> None:
    """A logged-in human keeps working in both modes — which is also what guarantees the
    superuser can always reach the panel to flip the switch back."""
    uid = pool.add_human("anton")
    cookie = {"uid": uid, "ep": 0, "iat": int(time.time())}

    for mode in ("public", "private"):
        pool.set_mode(mode)
        user = await current_user(make_request(dict(cookie)))
        assert user.id == uid
        assert user.kind == "human"


async def test_a_humans_request_never_reads_the_access_mode(pool: FakePool) -> None:
    """The kill switch is uncached, so it costs a query. A valid human session does not need
    it — the answer is the same in both modes — so it must not pay for it."""
    uid = pool.add_human("anton")
    pool.set_mode("public")
    cookie = {"uid": uid, "ep": 0, "iat": int(time.time())}

    before = pool.count_statements("access_mode")
    await current_user(make_request(dict(cookie)))
    assert pool.count_statements("access_mode") == before


async def test_a_revoked_session_epoch_still_wins_in_public_mode(pool: FakePool) -> None:
    """Revocation must not be weakened by the new branches: a stale `ep` invalidates the
    session, and in public mode the holder is demoted to a brand-new guest rather than being
    handed back the human account `set-password` or `disable-user` just locked."""
    uid = pool.add_human("anton", epoch=7)
    pool.set_mode("public")

    user = await current_user(make_request({"uid": uid, "ep": 6, "iat": int(time.time())}))
    assert user.id != uid
    assert user.kind == "guest"


async def test_a_disabled_human_is_demoted_not_admitted(pool: FakePool) -> None:
    uid = pool.add_human("anton", active=False)
    pool.set_mode("public")

    user = await current_user(make_request({"uid": uid, "ep": 0, "iat": int(time.time())}))
    assert user.kind == "guest"
    assert user.id != uid

    pool.set_mode("private")
    with pytest.raises(HTTPException):
        await current_user(make_request({"uid": uid, "ep": 0, "iat": int(time.time())}))


# --------------------------------------------------------------------------------------
# last_seen_at: cheap enough to keep
# --------------------------------------------------------------------------------------


async def test_last_seen_is_written_once_per_sliding_window_not_once_per_request(
    pool: FakePool,
) -> None:
    pool.set_mode("public")
    first = make_request()
    await current_user(first)
    cookie = cookie_of(first)

    before = pool.count_statements("last_seen_at")
    for _ in range(10):
        await current_user(make_request(dict(cookie)))
    assert pool.count_statements("last_seen_at") == before, "one UPDATE per request"

    # A day later, the cookie is re-signed — and the activity stamp rides along with it.
    stale_iat = int(time.time()) - _DAY - 1
    request = make_request(dict(cookie, iat=stale_iat))
    await current_user(request)
    assert pool.count_statements("last_seen_at") == before + 1
    assert request.session["iat"] > stale_iat, "the cookie window slid too"

    # And the refreshed cookie does not pay for it again on the requests that follow.
    for _ in range(5):
        await current_user(make_request(cookie_of(request)))
    assert pool.count_statements("last_seen_at") == before + 1


async def test_a_failed_last_seen_update_does_not_fail_the_request(
    pool: FakePool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bookkeeping on the auth path must never be able to log a visitor out."""
    pool.set_mode("public")
    first = make_request()
    guest = await current_user(first)
    cookie = dict(cookie_of(first), iat=int(time.time()) - _DAY - 1)

    async def _boom(sql: str, *args: Any) -> str:
        if "last_seen_at" in sql:
            raise asyncpg.PostgresConnectionError("nope")
        return "OK"

    monkeypatch.setattr(pool, "execute", _boom)
    assert (await current_user(make_request(cookie))).id == guest.id


# --------------------------------------------------------------------------------------
# The hidden panel
# --------------------------------------------------------------------------------------


async def test_require_superuser_admits_a_superuser(pool: FakePool) -> None:
    uid = pool.add_human("root", superuser=True)
    user = await require_superuser(make_request({"uid": uid, "ep": 0, "iat": int(time.time())}))
    assert user.id == uid
    assert user.is_superuser
    assert user.kind == "human"


@pytest.mark.parametrize("mode", ["public", "private"])
async def test_a_superuser_reaches_the_panel_in_either_mode(pool: FakePool, mode: str) -> None:
    """Otherwise flipping to public would be a one-way door."""
    uid = pool.add_human("root", superuser=True)
    pool.set_mode(mode)
    assert (
        await require_superuser(make_request({"uid": uid, "ep": 0, "iat": int(time.time())}))
    ).id == uid


async def test_require_superuser_is_404_for_a_normal_user(pool: FakePool) -> None:
    uid = pool.add_human("anton")
    with pytest.raises(HTTPException) as caught:
        await require_superuser(make_request({"uid": uid, "ep": 0, "iat": int(time.time())}))
    assert caught.value.status_code == 404, "403 would confirm the route exists"


async def test_require_superuser_is_404_for_a_guest(pool: FakePool) -> None:
    pool.set_mode("public")
    request = make_request()
    await current_user(request)

    with pytest.raises(HTTPException) as caught:
        await require_superuser(make_request(cookie_of(request)))
    assert caught.value.status_code == 404


async def test_require_superuser_is_404_for_an_anonymous_prober_and_mints_nothing(
    pool: FakePool,
) -> None:
    """In public mode `current_user` mints on sight. `require_superuser` must not: a hidden
    URL that inserts a row per probe is a way to fill `app_user` from the outside."""
    pool.set_mode("public")

    with pytest.raises(HTTPException) as caught:
        await require_superuser(make_request())
    assert caught.value.status_code == 404
    assert pool.user_count() == 0


async def test_require_superuser_is_404_for_a_disabled_superuser(pool: FakePool) -> None:
    uid = pool.add_human("root", superuser=True, active=False)
    with pytest.raises(HTTPException) as caught:
        await require_superuser(make_request({"uid": uid, "ep": 0, "iat": int(time.time())}))
    assert caught.value.status_code == 404


async def test_require_superuser_is_404_for_a_revoked_session(pool: FakePool) -> None:
    uid = pool.add_human("root", superuser=True, epoch=3)
    request = make_request({"uid": uid, "ep": 2, "iat": int(time.time())})
    with pytest.raises(HTTPException) as caught:
        await require_superuser(request)
    assert caught.value.status_code == 404
    assert request.session == {}


async def test_a_guest_row_that_somehow_carried_the_flag_is_still_404(pool: FakePool) -> None:
    """Nothing in the codebase sets `is_superuser` on a guest — the mint hardcodes the
    default and the CLI refuses. This asserts the belt-and-braces `kind == 'human'` check
    that stops a future INSERT copying the wrong template from becoming a privilege bug."""
    pool.set_mode("public")
    request = make_request()
    guest = await current_user(request)
    pool._conn.execute("UPDATE app_user SET is_superuser = 1 WHERE id = ?", (guest.id,))
    pool._conn.commit()

    with pytest.raises(HTTPException) as caught:
        await require_superuser(make_request(cookie_of(request)))
    assert caught.value.status_code == 404


async def test_resolve_session_never_mints_and_never_raises(pool: FakePool) -> None:
    """The helper both `current_user` and `require_superuser` are built on: it answers
    'who is this, if anyone' and has no opinion about what to do when the answer is nobody."""
    pool.set_mode("public")
    assert await resolve_session(make_request()) is None
    assert await resolve_session(make_request({"uid": 999, "ep": 0, "iat": 0})) is None
    assert pool.user_count() == 0

    uid = pool.add_human("anton")
    row = await resolve_session(make_request({"uid": uid, "ep": 0, "iat": int(time.time())}))
    assert row is not None
    assert row["username"] == "anton"
