"""User management and tariff ingest. The ONLY way a user comes into existence.

    docker compose exec app python -m app.cli create-user anton
    docker compose exec app python -m app.cli set-password anton      # also revokes sessions
    docker compose exec app python -m app.cli disable-user anton      # deactivate + revoke
    docker compose exec app python -m app.cli list-users
    docker compose run --rm app python -m app.cli ingest data/uktzed_hierarchical.json

There is no registration route in the application, so this file is the complete account
lifecycle. `set-password` and `disable-user` both bump `session_epoch`, which is what turns
"disabled" from advisory into enforced: every outstanding signed cookie for that user fails
its next request. (Rotating SESSION_SECRET invalidates every session for every user — the
break-glass option.)
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
        "SELECT id, username, is_active FROM app_user WHERE username = $1", username
    )
    if row is None:
        raise LookupError(username)
    return row


@cli.command("create-user")
def create_user(
    username: str,
    password: Annotated[
        str | None,
        typer.Option(help="Omit to generate a strong random password and print it once."),
    ] = None,
    display_name: Annotated[str | None, typer.Option(help="Defaults to the username.")] = None,
) -> None:
    """Create an account. Usernames are case-insensitive (CITEXT) and must be unique."""
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
            "INSERT INTO app_user (username, password_hash, display_name) "
            "VALUES ($1, $2, $3) RETURNING id",
            username,
            pw_hash,
            display_name or username,
        )

    try:
        user_id = _run(_work)
    except asyncpg.UniqueViolationError:
        _fail(f"user {username!r} already exists")

    typer.secho(f"created user {username!r} (id={user_id})", fg=typer.colors.GREEN)
    if generated:
        typer.secho(f"password: {password}   (shown once)", fg=typer.colors.YELLOW)


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
def list_users() -> None:
    """List every account, with its thread count."""

    async def _work() -> list[asyncpg.Record]:
        return await get_pool().fetch(
            """
            SELECT u.id, u.username, u.display_name, u.is_active, u.session_epoch,
                   u.last_login_at, count(t.id) AS threads
              FROM app_user u
              LEFT JOIN chat_thread t ON t.user_id = u.id
             GROUP BY u.id
             ORDER BY u.id
            """
        )

    rows = _run(_work)
    if not rows:
        typer.echo("no users yet — create one with: python -m app.cli create-user <username>")
        return

    header = f"{'id':>4}  {'username':<24} {'state':<9} {'ep':>3}  {'threads':>7}  last login"
    typer.echo(header)
    typer.echo("-" * len(header))
    for u in rows:
        state = "active" if u["is_active"] else "DISABLED"
        last = u["last_login_at"].strftime("%Y-%m-%d %H:%M") if u["last_login_at"] else "never"
        typer.echo(
            f"{u['id']:>4}  {u['username']:<24} {state:<9} {u['session_epoch']:>3}  "
            f"{u['threads']:>7}  {last}"
        )


if __name__ == "__main__":
    cli()
