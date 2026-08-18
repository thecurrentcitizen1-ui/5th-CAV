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
                    guild_id BIGINT NOT NULL,
                    discord_user_id BIGINT NOT NULL,
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
            await self.db.execute("""
                CREATE TABLE IF NOT EXISTS voice_sessions (
                    id BIGSERIAL PRIMARY KEY,
                    guild_id BIGINT NOT NULL,
                    discord_user_id BIGINT NOT NULL,
                    username TEXT,
                    display_name TEXT,
                    channel_id TEXT,
                    channel_name TEXT,
                    started_at TIMESTAMPTZ NOT NULL,
                    ended_at TIMESTAMPTZ NOT NULL,
                    duration_seconds INTEGER NOT NULL DEFAULT 0,
                    close_reason TEXT,
                    recovered_after_restart BOOLEAN NOT NULL DEFAULT FALSE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            await self.db.execute("CREATE INDEX IF NOT EXISTS idx_voice_sessions_member ON voice_sessions(guild_id,discord_user_id,ended_at DESC)")
            await self.db.execute("""CREATE TABLE IF NOT EXISTS activity_voice_channels (guild_id BIGINT NOT NULL, channel_id BIGINT NOT NULL, channel_name TEXT, added_by BIGINT, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), PRIMARY KEY(guild_id,channel_id))""")
            log.info("[SCHEMA READY] discord_members and voice_sessions verified")
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
            if event_type == "voice_session":
                await self.db.execute("""
                    INSERT INTO voice_sessions
                    (guild_id,discord_user_id,username,display_name,channel_id,channel_name,started_at,ended_at,duration_seconds,close_reason,recovered_after_restart)
                    VALUES($1,$2,$3,$4,$5,$6,$7::timestamptz,$8::timestamptz,$9,$10,$11)
                """, int(payload.get("guild_id")), int(payload.get("discord_user_id")),
                     payload.get("username"), payload.get("display_name"),
                     str(payload.get("channel_id") or ""), payload.get("channel_name"),
                     payload.get("started_at"), payload.get("ended_at"),
                     int(payload.get("duration_seconds") or 0), payload.get("close_reason"),
                     bool(payload.get("recovered_after_restart")))
                # Ten minutes in community voice is a useful activity signal even
                # when the session is not long enough to earn formal event credit.
                # It protects the Soldier from inactivity degradation without
                # awarding operations, promotions, or attendance by itself.
                if int(payload.get("duration_seconds") or 0) >= 600:
                    qualifying = await self.db.fetchrow("SELECT 1 FROM activity_voice_channels WHERE guild_id=$1 AND channel_id=$2", int(payload.get("guild_id")), int(payload.get("channel_id")))
                    if qualifying:
                        await self.db.execute("""UPDATE personnel p SET activity_last_seen_at=NOW(),updated_at=NOW() FROM website_member_links w WHERE w.personnel_id=p.id::text AND w.guild_id::text=$1 AND w.discord_user_id::text=$2""", str(payload.get("guild_id")), str(payload.get("discord_user_id")))
                        await self.db.execute("""INSERT INTO personnel_activity_credit(personnel_id,source,source_reference,activity_type,activity_date,duration_seconds,credited) SELECT p.id,'DISCORD_VOICE',$3,'COMMUNITY ACTIVITY',CURRENT_DATE,$4,TRUE FROM personnel p JOIN website_member_links w ON w.personnel_id=p.id::text WHERE w.guild_id::text=$1 AND w.discord_user_id::text=$2 ON CONFLICT DO NOTHING""", str(payload.get("guild_id")), str(payload.get("discord_user_id")), str(payload.get("channel_id")), int(payload.get("duration_seconds") or 0))

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
