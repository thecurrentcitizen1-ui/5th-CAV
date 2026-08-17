import logging
from datetime import datetime, timezone
from database import Database

log = logging.getLogger("battalion-clerk.collector")

class DataCollector:
    def __init__(self):
        self.db = Database()
        self.started = False

    async def start(self):
        if self.started:
            return
        await self.db.connect()
        if self.db.pool:
            await self.db.execute("""
                CREATE TABLE IF NOT EXISTS clerk_events (
                    id BIGSERIAL PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    payload JSONB NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            await self.db.execute("""
                CREATE TABLE IF NOT EXISTS discord_members (
                    guild_id TEXT NOT NULL,
                    discord_user_id TEXT NOT NULL,
                    username TEXT,
                    display_name TEXT,
                    is_bot BOOLEAN NOT NULL DEFAULT FALSE,
                    active BOOLEAN NOT NULL DEFAULT TRUE,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (guild_id, discord_user_id)
                )
            """)

            # Existing Railway databases may already have discord_members from an
            # earlier Battalion Clerk build. CREATE TABLE IF NOT EXISTS does not
            # add newly introduced columns, so migrate the live table safely.
            await self.db.execute("""
                ALTER TABLE discord_members
                ADD COLUMN IF NOT EXISTS username TEXT
            """)
            await self.db.execute("""
                ALTER TABLE discord_members
                ADD COLUMN IF NOT EXISTS display_name TEXT
            """)
            await self.db.execute("""
                ALTER TABLE discord_members
                ADD COLUMN IF NOT EXISTS is_bot BOOLEAN NOT NULL DEFAULT FALSE
            """)
            await self.db.execute("""
                ALTER TABLE discord_members
                ADD COLUMN IF NOT EXISTS active BOOLEAN NOT NULL DEFAULT TRUE
            """)
            await self.db.execute("""
                ALTER TABLE discord_members
                ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            """)
            log.info("[SCHEMA READY] discord_members columns verified")
        self.started = True
        log.info("[COLLECTOR READY] postgres-configured=%s", bool(self.db.pool))

    async def record_event(self, event_type, payload):
        if not self.started:
            await self.start()
        if self.db.pool:
            await self.db.execute(
                "INSERT INTO clerk_events (event_type, payload) VALUES ($1, $2::jsonb)",
                event_type, __import__("json").dumps(payload)
            )

    async def upsert_member(self, member):
        if not self.started:
            await self.start()
        if self.db.pool:
            await self.db.execute("""
                INSERT INTO discord_members
                    (guild_id, discord_user_id, username, display_name, is_bot, active, updated_at)
                VALUES ($1,$2,$3,$4,$5,TRUE,NOW())
                ON CONFLICT (guild_id, discord_user_id)
                DO UPDATE SET username=EXCLUDED.username,
                              display_name=EXCLUDED.display_name,
                              is_bot=EXCLUDED.is_bot,
                              active=TRUE,
                              updated_at=NOW()
            """, member.guild.id, member.id, member.name,
                 member.display_name, member.bot)

    async def mark_member_left(self, member, when=None):
        if not self.started:
            await self.start()
        if self.db.pool:
            await self.db.execute("""
                UPDATE discord_members
                SET active=FALSE, updated_at=NOW()
                WHERE guild_id=$1 AND discord_user_id=$2
            """, member.guild.id, member.id)
