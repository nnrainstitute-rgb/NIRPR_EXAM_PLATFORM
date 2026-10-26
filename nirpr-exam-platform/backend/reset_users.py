"""Explicit, backed-up user reset. Never invoked by application startup."""
import argparse
import asyncio
from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess

from dotenv import load_dotenv
from sqlalchemy import MetaData, delete, func, select, update
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.engine import make_url


def reset_plan(metadata):
    """Keep content; remove tables that require a deleted user/dependent row."""
    removed = {"users"}
    changed = True
    while changed:
        changed = False
        for table in metadata.tables.values():
            if table.name not in removed and any(
                not fk.parent.nullable and fk.column.table.name in removed
                for fk in table.foreign_keys
            ):
                removed.add(table.name)
                changed = True
    return removed


async def reset(url, execute=False, backup_dir=None, pg_dump="pg_dump"):
    parsed = make_url(url)
    if parsed.drivername in ("postgres", "postgresql"):
        parsed = parsed.set(drivername="postgresql+asyncpg")
    engine = create_async_engine(parsed, connect_args={"timeout": 10} if parsed.drivername.startswith("postgresql") else {})
    try:
        async with engine.begin() as conn:
            metadata = MetaData()
            await conn.run_sync(metadata.reflect)
            if "users" not in metadata.tables:
                raise RuntimeError("No users table; database was not changed.")
            removed = reset_plan(metadata)
            counts = {table.name: (await conn.execute(select(func.count()).select_from(table))).scalar_one()
                      for table in metadata.sorted_tables if table.name in removed}
            print("Target:", parsed.drivername, parsed.host or "local file", parsed.database)
            print("Rows to remove:", counts)
            print("Preserving content tables:", sorted(set(metadata.tables) - removed))
        if not execute:
            print("Preview only. Use --execute to back up and clear these records.")
            return counts
        folder = Path(backup_dir or Path(__file__).parent / "backups").resolve()
        folder.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        if parsed.drivername.startswith("postgresql"):
            backup = folder / f"before-user-reset-{stamp}.dump"
            env = os.environ.copy()
            env.update(PGHOST=parsed.host or "localhost", PGPORT=str(parsed.port or 5432),
                       PGUSER=parsed.username or "postgres", PGPASSWORD=parsed.password or "",
                       PGDATABASE=parsed.database, PGCONNECT_TIMEOUT="10")
            subprocess.run([pg_dump, "--format=custom", "--file", str(backup)], env=env, check=True)
        elif parsed.drivername.startswith("sqlite") and parsed.database != ":memory:":
            backup = folder / f"before-user-reset-{stamp}.sqlite3"
            with sqlite3.connect(parsed.database) as source, sqlite3.connect(backup) as dest:
                source.backup(dest)
        else:
            raise RuntimeError("Reset requires a persistent PostgreSQL or SQLite database.")
        if backup.stat().st_size == 0:
            raise RuntimeError("Empty backup; database was not changed.")
        print("Private backup:", backup)
        async with engine.begin() as conn:
            if conn.dialect.name == "postgresql":
                await conn.exec_driver_sql("SET LOCAL lock_timeout = '10s'")
                # Serialize the reset with application writes and fail rather than wait indefinitely.
                names = ', '.join(conn.dialect.identifier_preparer.quote(t.name) for t in metadata.sorted_tables)
                await conn.exec_driver_sql(f"LOCK TABLE {names} IN ACCESS EXCLUSIVE MODE")
            for table in metadata.tables.values():
                for fk in table.foreign_keys:
                    if fk.parent.nullable and fk.column.table.name in removed:
                        await conn.execute(update(table).values({fk.parent.name: None}))
            for table in reversed(metadata.sorted_tables):
                if table.name in removed:
                    await conn.execute(delete(table))
            remaining = (await conn.execute(select(func.count()).select_from(metadata.tables['users']))).scalar_one()
            if remaining:
                raise RuntimeError("Reset verification failed; transaction rolled back.")
        print("Verified: 0 users remain. Create a new first Super Admin before use.")
        return counts
    finally:
        await engine.dispose()


def main():
    load_dotenv(Path(__file__).parent / ".env")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--backup-dir")
    parser.add_argument("--pg-dump", default=shutil.which("pg_dump") or "pg_dump")
    args = parser.parse_args()
    url = os.environ.get("DATABASE_URL", "sqlite+aiosqlite:///./nirpr_exam.db")
    asyncio.run(reset(url, args.execute, args.backup_dir, args.pg_dump))


if __name__ == "__main__":
    main()
