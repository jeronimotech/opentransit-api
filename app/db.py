import logging
from pathlib import Path

import asyncpg

from .config import settings

log = logging.getLogger("ot.db")
_pool: asyncpg.Pool | None = None


def pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("database pool not initialised")
    return _pool


async def init_pool() -> None:
    global _pool
    _pool = await asyncpg.create_pool(settings().DATABASE_URL, min_size=2, max_size=10,
                                      command_timeout=120)
    schema = (Path(__file__).parent / "sql" / "001_schema.sql").read_text()
    async with _pool.acquire() as c:
        await c.execute(schema)
    log.info("database ready")


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


async def active_feed_version(city: str) -> int | None:
    async with pool().acquire() as c:
        return await c.fetchval("SELECT id FROM feed_version WHERE city=$1 AND is_active LIMIT 1", city)
