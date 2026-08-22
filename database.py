import os
import asyncpg
import logging

log = logging.getLogger("battalion-clerk.database")

class Database:
    def __init__(self):
        self.url = os.getenv("DATABASE_URL")
        self.pool = None

    async def connect(self):
        if not self.url:
            log.warning("[POSTGRES DISABLED] DATABASE_URL is not set")
            return
        if self.pool is None:
            self.pool = await asyncpg.create_pool(self.url, min_size=1, max_size=5)
            async with self.pool.acquire() as conn:
                version = await conn.fetchval("SELECT version()")
            log.info("[POSTGRES READY] %s", version)

    async def execute(self, query, *args):
        if not self.pool:
            return None
        return await self.pool.execute(query, *args)

    async def fetch(self, query, *args):
        if not self.pool:
            return []
        return await self.pool.fetch(query, *args)

    async def fetchrow(self, query, *args):
        if not self.pool:
            return None
        return await self.pool.fetchrow(query, *args)

    async def close(self):
        if self.pool:
            await self.pool.close()
            self.pool = None
