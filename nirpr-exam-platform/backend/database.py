"""
NIRPR RSO Examination Platform - Database Configuration
"""

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy import inspect, text
from sqlalchemy.orm import declarative_base
from sqlalchemy.pool import NullPool
from contextlib import asynccontextmanager
from name_utils import split_full_name
import os

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./nirpr_exam.db")
if DATABASE_URL.startswith(("postgres://", "postgresql://")):
    DATABASE_URL = "postgresql+asyncpg://" + DATABASE_URL.split("://", 1)[1]

# Release idle connections so Railway Serverless can sleep between visits.
# Use the normal pool during busy examination windows if measurements warrant it.
engine_options = {}
if DATABASE_URL.startswith("postgresql+asyncpg://") and os.getenv("DB_POOL_MODE", "").lower() == "null":
    engine_options["poolclass"] = NullPool

engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    future=True,
    pool_pre_ping=True,
    **engine_options,
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)

Base = declarative_base()


async def init_db():
    """Initialize database tables."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # create_all does not add columns to an existing installation.
        columns = await conn.run_sync(lambda sync_conn: {
            column["name"] for column in inspect(sync_conn).get_columns("users")
        })
        if "must_change_password" not in columns:
            await conn.execute(text(
                "ALTER TABLE users ADD COLUMN must_change_password BOOLEAN NOT NULL DEFAULT FALSE"
            ))
        for name, length in (("surname", 100), ("first_name", 100), ("other_name", 150)):
            if name not in columns:
                await conn.execute(text(f"ALTER TABLE users ADD COLUMN {name} VARCHAR({length})"))
        if "full_name" in columns:
            rows = (await conn.execute(text(
                "SELECT id, full_name FROM users WHERE surname IS NULL OR first_name IS NULL"
            ))).all()
            for user_id, full_name in rows:
                surname, first_name, other_name = split_full_name(full_name or "")
                await conn.execute(text(
                    "UPDATE users SET surname=:surname, first_name=:first_name, other_name=:other_name WHERE id=:id"
                ), {"surname": surname, "first_name": first_name, "other_name": other_name, "id": user_id})
        missing = (await conn.execute(text(
            "SELECT count(*) FROM users WHERE surname IS NULL OR first_name IS NULL"
        ))).scalar_one()
        if missing:
            raise RuntimeError(f"Cannot remove full_name: {missing} users have incomplete name parts")
        if "full_name" in columns:
            await conn.execute(text("ALTER TABLE users DROP COLUMN full_name"))
        if conn.dialect.name == "postgresql":
            current_columns = await conn.run_sync(lambda sync_conn: {
                column["name"]: column for column in inspect(sync_conn).get_columns("users")
            })
            for name in ("surname", "first_name"):
                if current_columns[name]["nullable"]:
                    await conn.execute(text(f"ALTER TABLE users ALTER COLUMN {name} SET NOT NULL"))


async def get_db() -> AsyncSession:
    """Dependency to get database session."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()


@asynccontextmanager
async def get_db_context():
    """Context manager for database sessions."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
