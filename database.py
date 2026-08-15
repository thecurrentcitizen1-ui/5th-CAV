import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import asyncpg

log = logging.getLogger('battalion-clerk.database')

DATABASE_URL = os.getenv('DATABASE_URL', '').strip()


class Database:
    """Persistent PostgreSQL store for Battalion Clerk.

    This database is deliberately data-centric. It stores Discord identity,
    raw Discord events, and completed voice sessions. Website business logic
    (rank, awards, DEROS, readiness, etc.) does not live here.
    """

    def __init__(self) -> None:
        self.pool: Optional[asyncpg.Pool] = None

    @property
    def enabled(self) -> bool:
        return bool(DATABASE_URL)

    async def connect(self) -> None:
        if not self.enabled:
            log.warning('[POSTGRES DISABLED] DATABASE_URL is not set; using local safety buffer only.')
            return

        self.pool = await asyncpg.create_pool(
            dsn=DATABASE_URL,
            min_size=1,
            max_size=5,
            command_timeout=15,
        )
        await self._init_schema()
        async with self.pool.acquire() as conn:
            version = await conn.fetchval('SELECT version()')
        log.info('[POSTGRES READY] %s', str(version).split(',')[0])

    async def close(self) -> None:
        if self.pool:
            await self.pool.close()
            self.pool = None

    async def _init_schema(self) -> None:
        assert self.pool is not None
        sql = r'''
        CREATE TABLE IF NOT EXISTS discord_members (
            guild_id BIGINT NOT NULL,
            discord_user_id BIGINT NOT NULL,
            username TEXT NOT NULL,
            display_name TEXT NOT NULL,
            is_bot BOOLEAN NOT NULL DEFAULT FALSE,
            joined_at TIMESTAMPTZ NULL,
            left_at TIMESTAMPTZ NULL,
            last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (guild_id, discord_user_id)
        );

        CREATE TABLE IF NOT EXISTS discord_events (
            id BIGSERIAL PRIMARY KEY,
            event_type TEXT NOT NULL,
            guild_id BIGINT NULL,
            discord_user_id BIGINT NULL,
            payload JSONB NOT NULL,
            occurred_at TIMESTAMPTZ NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS idx_discord_events_user_time
            ON discord_events (guild_id, discord_user_id, occurred_at DESC);
        CREATE INDEX IF NOT EXISTS idx_discord_events_type_time
            ON discord_events (event_type, occurred_at DESC);

        CREATE TABLE IF NOT EXISTS voice_sessions (
            id BIGSERIAL PRIMARY KEY,
            guild_id BIGINT NOT NULL,
            discord_user_id BIGINT NOT NULL,
            channel_id BIGINT NOT NULL,
            channel_name TEXT NOT NULL,
            started_at TIMESTAMPTZ NOT NULL,
            ended_at TIMESTAMPTZ NOT NULL,
            duration_seconds INTEGER NOT NULL CHECK (duration_seconds >= 0),
            close_reason TEXT NOT NULL,
            recovered_after_restart BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS idx_voice_sessions_user_start
            ON voice_sessions (guild_id, discord_user_id, started_at DESC);
        CREATE INDEX IF NOT EXISTS idx_voice_sessions_channel_start
            ON voice_sessions (guild_id, channel_id, started_at DESC);

        -- Reserved bridge for the future website personnel record.
        -- Battalion Clerk does not populate this automatically.
        CREATE TABLE IF NOT EXISTS website_member_links (
            guild_id BIGINT NOT NULL,
            discord_user_id BIGINT NOT NULL,
            personnel_id TEXT NOT NULL,
            linked_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (guild_id, discord_user_id),
            UNIQUE (personnel_id)
        );
        '''
        async with self.pool.acquire() as conn:
            await conn.execute(sql)

    async def upsert_member(
        self,
        *,
        guild_id: int,
        discord_user_id: int,
        username: str,
        display_name: str,
        is_bot: bool,
        joined_at: Optional[datetime],
        left_at: Optional[datetime] = None,
    ) -> None:
        if not self.pool:
            return
        now = datetime.now(timezone.utc)
        async with self.pool.acquire() as conn:
            await conn.execute(
                '''
                INSERT INTO discord_members (
                    guild_id, discord_user_id, username, display_name, is_bot,
                    joined_at, left_at, last_seen_at, updated_at
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$8)
                ON CONFLICT (guild_id, discord_user_id) DO UPDATE SET
                    username = EXCLUDED.username,
                    display_name = EXCLUDED.display_name,
                    is_bot = EXCLUDED.is_bot,
                    joined_at = COALESCE(discord_members.joined_at, EXCLUDED.joined_at),
                    left_at = EXCLUDED.left_at,
                    last_seen_at = EXCLUDED.last_seen_at,
                    updated_at = EXCLUDED.updated_at
                ''',
                guild_id, discord_user_id, username, display_name, is_bot,
                joined_at, left_at, now,
            )

    async def mark_member_left(self, *, guild_id: int, discord_user_id: int, left_at: datetime) -> None:
        if not self.pool:
            return
        async with self.pool.acquire() as conn:
            await conn.execute(
                '''
                UPDATE discord_members
                SET left_at=$3, last_seen_at=$3, updated_at=$3
                WHERE guild_id=$1 AND discord_user_id=$2
                ''',
                guild_id, discord_user_id, left_at,
            )

    async def record_event(self, event_type: str, payload: Dict[str, Any], occurred_at: datetime) -> None:
        if not self.pool:
            return
        guild_id = _to_int(payload.get('guild_id'))
        discord_user_id = _to_int(payload.get('discord_user_id'))
        async with self.pool.acquire() as conn:
            await conn.execute(
                '''
                INSERT INTO discord_events (event_type, guild_id, discord_user_id, payload, occurred_at)
                VALUES ($1,$2,$3,$4::jsonb,$5)
                ''',
                event_type, guild_id, discord_user_id, json.dumps(payload), occurred_at,
            )

    async def record_voice_session(self, payload: Dict[str, Any]) -> None:
        if not self.pool:
            return
        async with self.pool.acquire() as conn:
            await conn.execute(
                '''
                INSERT INTO voice_sessions (
                    guild_id, discord_user_id, channel_id, channel_name,
                    started_at, ended_at, duration_seconds, close_reason,
                    recovered_after_restart
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
                ''',
                int(payload['guild_id']),
                int(payload['discord_user_id']),
                int(payload['channel_id']),
                payload['channel_name'],
                _to_dt(payload['started_at']),
                _to_dt(payload['ended_at']),
                int(payload['duration_seconds']),
                payload['close_reason'],
                bool(payload.get('recovered_after_restart')),
            )


def _to_int(value: Any) -> Optional[int]:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _to_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    text = str(value)
    if text.endswith('Z'):
        text = text[:-1] + '+00:00'
    dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
