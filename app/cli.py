"""User management and tariff ingest. The ONLY way a user comes into existence.

    docker compose exec app python -m app.cli create-user anton
    docker compose exec app python -m app.cli create-user anton --superuser
    docker compose exec app python -m app.cli set-password anton      # also revokes sessions
    docker compose exec app python -m app.cli disable-user anton      # deactivate + revoke
    docker compose exec app python -m app.cli grant-superuser anton
    docker compose exec app python -m app.cli revoke-superuser anton
    docker compose exec app python -m app.cli purge-guests --older-than-days 30
    docker compose exec app python -m app.cli list-users
    docker compose run --rm app python -m app.cli ingest data/uktzed_hierarchical.json

There is no registration route in the application, so this file is the complete account
lifecycle for HUMANS. `set-password` and `disable-user` both bump `session_epoch`, which is
what turns "disabled" from advisory into enforced: every outstanding signed cookie for that
user fails its next request. (Rotating SESSION_SECRET invalidates every session for every
user — the break-glass option.)

Guests are the exception, and the only rows this file does not create: they are minted by
`app/auth/guest.py` when the runtime access mode is 'public', and they are removed by
`purge-guests`. There is deliberately no `create-guest` — a guest with no browser session
pointing at it is an orphan, so minting one from a terminal would be minting garbage.

The FIRST superuser can only be created here. `POST /api/admin/users/{id}/superuser` exists
and a superuser can promote anyone with it — but reaching that route already requires being
one, so the chain has to start with shell access to the container. Nothing on the network can
begin it, and `require_superuser` refuses guests on top of the flag.
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated, NoReturn

import asyncpg
import typer

from app.auth.passwords import hash_password
from app.db import acquire, close_pool, get_pool, init_pool
from app.settings import get_settings
from app.tariff.ingest import IngestError, IngestResult, ingest_file

cli = typer.Typer(help="UKTZED classifier user management and tariff ingest", no_args_is_help=True)

_DEFAULT_TARIFF = Path("data/uktzed_hierarchical.json")


def _run[T](work: Callable[[], Awaitable[T]]) -> T:
    """Open the pool, run one coroutine, close the pool. The CLI is a separate process from
    the web app, so it owns its own pool for the duration of a single command."""

    async def _main() -> T:
        await init_pool(get_settings().database_url, min_size=1, max_size=2)
        try:
            return await work()
        finally:
            await close_pool()

    return asyncio.run(_main())


def _fail(message: str) -> NoReturn:
    typer.secho(message, fg=typer.colors.RED, err=True)
    raise typer.Exit(code=1)


async def _require_user(username: str) -> asyncpg.Record:
    row = await get_pool().fetchrow(
        "SELECT id, username, is_active, kind, is_superuser FROM app_user WHERE username = $1",
        username,
    )
    if row is None:
        raise LookupError(username)
    return row


async def _set_superuser(username: str, *, value: bool) -> str:
    """Flip `is_superuser`, refusing to do it to a guest.

    `session_epoch` is deliberately NOT bumped. `require_superuser` re-reads the row on every
    request, so a revoked admin loses the panel on their very next click; logging them out of
    the chat as well would be a side effect nobody asked for.
    """
    user = await _require_user(username)
    if user["kind"] != "human":
        raise PermissionError(user["username"])
    await get_pool().execute(
        "UPDATE app_user SET is_superuser = $2 WHERE id = $1", user["id"], value
    )
    return user["username"]


@cli.command("create-user")
def create_user(
    username: str,
    password: Annotated[
        str | None,
        typer.Option(help="Omit to generate a strong random password and print it once."),
    ] = None,
    display_name: Annotated[str | None, typer.Option(help="Defaults to the username.")] = None,
    superuser: Annotated[
        bool, typer.Option("--superuser", help="Also grant access to the hidden admin panel.")
    ] = False,
) -> None:
    """Create an account. Usernames are case-insensitive (CITEXT) and must be unique.

    A flag rather than a separate `create-superuser` command, so that there is exactly one
    account-creation path to audit and no chance of the two drifting apart. The rows created
    here always have kind='human'; guests come from `app/auth/guest.py` only.
    """
    username = username.strip()
    if not username:
        _fail("username must not be empty")

    generated = password is None
    if password is None:
        password = secrets.token_urlsafe(18)
    # Hashing is ~80 ms of CPU; do it before opening the pool, not inside the transaction.
    pw_hash = hash_password(password)

    async def _work() -> int:
        return await get_pool().fetchval(
            "INSERT INTO app_user (username, password_hash, display_name, kind, is_superuser) "
            "VALUES ($1, $2, $3, 'human', $4) RETURNING id",
            username,
            pw_hash,
            display_name or username,
            superuser,
        )

    try:
        user_id = _run(_work)
    except asyncpg.UniqueViolationError:
        _fail(f"user {username!r} already exists")

    role = "superuser" if superuser else "user"
    typer.secho(f"created {role} {username!r} (id={user_id})", fg=typer.colors.GREEN)
    if generated:
        typer.secho(f"password: {password}   (shown once)", fg=typer.colors.YELLOW)


@cli.command("grant-superuser")
def grant_superuser(username: str) -> None:
    """Give an existing human account access to the hidden admin panel."""
    try:
        name = _run(lambda: _set_superuser(username, value=True))
    except LookupError:
        _fail(f"no such user: {username!r}")
    except PermissionError as exc:
        _fail(f"{exc.args[0]!r} is a guest; guests can never be superusers")

    typer.secho(f"{name!r} is now a superuser", fg=typer.colors.GREEN)


@cli.command("revoke-superuser")
def revoke_superuser(username: str) -> None:
    """Take admin-panel access away. Effective on that user's next request."""
    try:
        name = _run(lambda: _set_superuser(username, value=False))
    except LookupError:
        _fail(f"no such user: {username!r}")
    except PermissionError as exc:
        _fail(f"{exc.args[0]!r} is a guest; guests can never be superusers")

    typer.secho(f"{name!r} is no longer a superuser", fg=typer.colors.GREEN)


@cli.command("purge-guests")
def purge_guests(
    older_than_days: Annotated[
        int,
        typer.Option(
            "--older-than-days",
            min=0,
            help="Delete guests last seen this many days ago. 0 deletes every guest.",
        ),
    ] = 30,
    yes: Annotated[bool, typer.Option("--yes", help="Skip the confirmation prompt.")] = False,
) -> None:
    """Delete stale guest accounts AND everything they produced.

    In public mode every visitor — and every crawler that follows a link and never comes
    back — leaves an `app_user` row behind, so this is the housekeeping the design trades
    for having guests be real users. It is the only command in this file that deletes data.

    The cascade is real, and was verified against the migrations rather than assumed:
    migration 0002 declares `chat_thread.user_id`, `chat_thread_item.user_id` and
    `chat_attachment.user_id` as `ON DELETE CASCADE`, and migration 0004 declares
    `classification.user_id` the same way, with `classification_code.classification_id` and
    `classification_tool_call.classification_id` cascading off `classification.id` in turn.
    One `DELETE FROM app_user` therefore removes that guest's threads, thread items,
    attachments, classifications, per-code rows and tool-call rows, with nothing orphaned
    and nothing left to clean up by hand. (`classification.thread_id` carries no FK by
    design, but it lives on a row that is itself cascaded, so it goes too.)

    `last_seen_at` is written at mint time and then at most once a day, so a guest who is
    actively chatting today is never within reach of `--older-than-days 1`.
    """

    async def _count() -> int:
        return await get_pool().fetchval(
            "SELECT count(*) FROM app_user "
            "WHERE kind = 'guest' "
            "  AND coalesce(last_seen_at, created_at) < now() - make_interval(days => $1::int)",
            older_than_days,
        )

    async def _delete() -> int:
        # RETURNING id and counting rows: `execute` gives back a "DELETE n" tag string that
        # would have to be parsed, and the count is wanted for the message either way.
        rows = await get_pool().fetch(
            "DELETE FROM app_user "
            "WHERE kind = 'guest' "
            "  AND coalesce(last_seen_at, created_at) < now() - make_interval(days => $1::int) "
            "RETURNING id",
            older_than_days,
        )
        return len(rows)

    doomed = _run(_count)
    if doomed == 0:
        typer.secho(f"no guests older than {older_than_days} day(s)", fg=typer.colors.GREEN)
        return

    window = "every guest account" if older_than_days == 0 else f"{doomed:,} guest account(s)"
    if not yes and not typer.confirm(
        f"delete {window} and all of their threads and classifications?"
    ):
        typer.secho("aborted", fg=typer.colors.YELLOW)
        raise typer.Exit(code=1)

    deleted = _run(_delete)
    typer.secho(
        f"purged {deleted:,} guest account(s) and their threads and classifications",
        fg=typer.colors.GREEN,
    )


@cli.command("set-password")
def set_password(
    username: str,
    password: Annotated[
        str,
        typer.Option(prompt=True, hide_input=True, confirmation_prompt=True),
    ],
) -> None:
    """Set a new password. Also bumps session_epoch, logging that user out everywhere."""
    pw_hash = hash_password(password)

    async def _work() -> str:
        user = await _require_user(username)
        await get_pool().execute(
            "UPDATE app_user SET password_hash = $2, session_epoch = session_epoch + 1 "
            "WHERE id = $1",
            user["id"],
            pw_hash,
        )
        return user["username"]

    try:
        name = _run(_work)
    except LookupError:
        _fail(f"no such user: {username!r}")

    typer.secho(f"password updated for {name!r}; all sessions invalidated", fg=typer.colors.GREEN)


@cli.command("disable-user")
def disable_user(username: str) -> None:
    """Deactivate an account and revoke its sessions immediately.

    Re-enabling is deliberately not a command — it is a one-line SQL UPDATE
    (`UPDATE app_user SET is_active = TRUE WHERE username = '…'`) and adding an
    `enable-user` verb would imply an account-state workflow this demo does not have.
    """

    async def _work() -> str:
        user = await _require_user(username)
        await get_pool().execute(
            "UPDATE app_user SET is_active = FALSE, session_epoch = session_epoch + 1 "
            "WHERE id = $1",
            user["id"],
        )
        return user["username"]

    try:
        name = _run(_work)
    except LookupError:
        _fail(f"no such user: {username!r}")

    typer.secho(f"disabled {name!r}; all sessions invalidated", fg=typer.colors.GREEN)


@cli.command("ingest")
def ingest(
    file: Annotated[
        Path, typer.Argument(help="Source JSON. Defaults to data/uktzed_hierarchical.json.")
    ] = _DEFAULT_TARIFF,
    activate: Annotated[
        bool, typer.Option(help="Make the loaded dataset the one the app serves.")
    ] = True,
) -> None:
    """Load the tariff JSON into Postgres. Idempotent per content sha256.

    Same function as `scripts/ingest.py` — this command exists because `make ingest`, the
    Dockerfile and the README all call `python -m app.cli ingest <path>`, and the container
    image is the only place the data and the driver are guaranteed to be together.

    `ingest_file` needs a `Connection`, not a `Pool` (`copy_records_to_table` is
    connection-level), so this borrows one out of the CLI's short-lived pool.
    """

    async def _work() -> IngestResult:
        async with acquire() as conn:
            return await ingest_file(conn, file, activate=activate)

    try:
        result = _run(_work)
    except IngestError as exc:
        # IngestInvariantError carries EVERY failure, so print it whole rather than repr()'d.
        _fail(f"ingest refused the file:\n{exc}")

    verb = "ingested" if result.created else "already present"
    typer.secho(
        f"dataset {result.dataset_id} {verb}: {result.node_count:,} nodes, "
        f"{result.terminal_count:,} terminals, sha256 {result.sha256}",
        fg=typer.colors.GREEN,
    )
    if result.activated:
        typer.secho("activated", fg=typer.colors.GREEN)


@cli.command("list-users")
def list_users(
    guests: Annotated[
        bool,
        typer.Option("--guests", help="Include guest accounts, which normally outnumber humans."),
    ] = False,
) -> None:
    """List accounts, with kind, superuser status and thread count.

    Guests are hidden by default and shown as a one-line total instead. After a public demo
    there can be thousands of them, and a listing that scrolls the humans off the screen is
    not a listing of the accounts anyone manages.
    """

    async def _work() -> tuple[list[asyncpg.Record], int]:
        pool = get_pool()
        rows = await pool.fetch(
            """
            SELECT u.id, u.username, u.display_name, u.is_active, u.is_superuser, u.kind,
                   u.session_epoch, u.last_login_at, u.last_seen_at, count(t.id) AS threads
              FROM app_user u
              LEFT JOIN chat_thread t ON t.user_id = u.id
             WHERE u.kind = 'human' OR $1::boolean
             GROUP BY u.id
             ORDER BY u.kind, u.id
            """,
            guests,
        )
        # Uses app_user_guest_idx from migration 0005 — an index-only count of the guests.
        total_guests = await pool.fetchval("SELECT count(*) FROM app_user WHERE kind = 'guest'")
        return rows, total_guests

    rows, total_guests = _run(_work)
    if not rows:
        typer.echo("no users yet — create one with: python -m app.cli create-user <username>")
        if total_guests:
            typer.echo(f"({total_guests:,} guest account(s); show them with --guests)")
        return

    header = (
        f"{'id':>6}  {'username':<24} {'kind':<6} {'role':<6} {'state':<9} "
        f"{'ep':>3}  {'threads':>7}  last active"
    )
    typer.echo(header)
    typer.echo("-" * len(header))
    for u in rows:
        state = "active" if u["is_active"] else "DISABLED"
        role = "ADMIN" if u["is_superuser"] else "-"
        # A guest never logs in, so last_login_at is always NULL for one; last_seen_at is the
        # column that means the same thing for both kinds.
        seen = u["last_seen_at"] or u["last_login_at"]
        last = seen.strftime("%Y-%m-%d %H:%M") if seen else "never"
        typer.echo(
            f"{u['id']:>6}  {u['username']:<24} {u['kind']:<6} {role:<6} {state:<9} "
            f"{u['session_epoch']:>3}  {u['threads']:>7}  {last}"
        )

    if not guests and total_guests:
        typer.echo("")
        typer.echo(f"{total_guests:,} guest account(s) hidden — show them with --guests")


if __name__ == "__main__":
    cli()
