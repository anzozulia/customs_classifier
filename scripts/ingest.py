#!/usr/bin/env python3
"""Load the tariff JSON into Postgres.

    python3 scripts/ingest.py                      # ingest and activate
    python3 scripts/ingest.py --check-only         # parse + assert, no database
    python3 scripts/ingest.py --no-activate        # load, leave the current dataset serving

Idempotent: re-running the same file inserts nothing and prints the dataset it already has.
A wrapper, not a program — everything it does lives in `app.tariff.ingest`, so the eval
harness and `app/cli.py` call the same function rather than shelling out to this.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.tariff.ingest import (  # noqa: E402
    IngestError,
    IngestResult,
    ingest_file,
    prepare,
)

DEFAULT_SOURCE = ROOT / "data" / "uktzed_hierarchical.json"


async def _ingest(source: Path, database_url: str, *, activate: bool) -> IngestResult:
    # Imported here, not at module scope, so --check-only runs with neither asyncpg nor a
    # settings file present.
    import asyncpg

    conn = await asyncpg.connect(database_url)
    try:
        return await ingest_file(conn, source, activate=activate)
    finally:
        await conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--file", type=Path, default=DEFAULT_SOURCE, help="source JSON")
    parser.add_argument("--database-url", default=None, help="overrides DATABASE_URL")
    parser.add_argument(
        "--no-activate",
        action="store_false",
        dest="activate",
        help="load without making the dataset the active one",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="parse and assert the invariants, touch no database",
    )
    args = parser.parse_args(argv)

    try:
        if args.check_only:
            digest, nodes = prepare(args.file)
            terminals = sum(1 for n in nodes if n.is_terminal)
            print(f"sha256    {digest}")
            print(f"nodes     {len(nodes):,}")
            print(f"terminals {terminals:,}")
            print("invariants OK")
            return 0

        database_url = args.database_url
        if database_url is None:
            from app.settings import get_settings

            database_url = get_settings().database_url

        result = asyncio.run(_ingest(args.file, database_url, activate=args.activate))
    except IngestError as exc:
        print(f"ingest refused the file:\n{exc}", file=sys.stderr)
        return 1

    verb = "ingested" if result.created else "already present"
    print(
        f"dataset {result.dataset_id} {verb}: {result.node_count:,} nodes, "
        f"{result.terminal_count:,} terminals, sha256 {result.sha256}"
    )
    if result.activated:
        print("activated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
