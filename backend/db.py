"""
db.py — PostgreSQL connection pool for Servix.
Manages asyncpg pool lifecycle and runs the initial schema migration on startup.
"""

import asyncpg
import os
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None


# ── Pool ──────────────────────────────────────────────────────────────────────

async def get_pool() -> asyncpg.Pool:
    """Return the active connection pool, raising if not yet initialised."""
    if _pool is None:
        raise RuntimeError("Database pool is not initialised. Call init_db() first.")
    return _pool


async def init_db() -> None:
    """
    Initialise the asyncpg connection pool and apply schema.sql.

    Safe to call multiple times — all CREATE statements use IF NOT EXISTS.
    """
    global _pool

    postgres_url = os.getenv("POSTGRES_URL")
    if not postgres_url:
        raise RuntimeError("POSTGRES_URL environment variable is not set.")

    _pool = await asyncpg.create_pool(
        dsn=postgres_url,
        min_size=2,
        max_size=10,
    )

    schema_path = Path(__file__).parent / "schema.sql"
    schema_sql = schema_path.read_text()

    async with _pool.acquire() as conn:
        await conn.execute(schema_sql)

    logger.info("Database initialised successfully.")


async def close_db() -> None:
    """Close the connection pool gracefully on shutdown."""
    global _pool
    if _pool:
        await _pool.close()
        _pool = None
        logger.info("Database pool closed.")
