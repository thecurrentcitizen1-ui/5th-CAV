import asyncio
import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import aiohttp

from database import Database

log = logging.getLogger('battalion-clerk.collector')

DB_PATH = os.getenv('LOCAL_DB_PATH', 'data/battalion_clerk.db')
WEBSITE_API_URL = os.getenv('WEBSITE_API_URL', '').strip()
WEBSITE_API_KEY = os.getenv('WEBSITE_API_KEY', '').strip()


class DataCollector:
    """Collects Discord activity and persists it safely.

    Persistence order:
      1. Local SQLite safety buffer
      2. Railway PostgreSQL (when DATABASE_URL is configured)
      3. Optional future website API forwarding

    PostgreSQL is the durable shared data store intended for the future website.
    """

    def __init__(self) -> None:
        path = Path(DB_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = str(path)
        self._init_db()
        self.database = Database()
        log.info('[BUFFER READY] SQLite=%s', self.db_path)

    async def start(self) -> None:
        await self.database.connect()
        log.info(
            '[COLLECTOR READY] postgres=%s | website_api=%s',
            'configured' if self.database.enabled else 'not configured',
            'configured' if WEBSITE_API_URL else 'not configured',
        )

    async def close(self) -> None:
        await self.database.close()

    def _connect(self):
        return sqlite3.connect(self.db_path)

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    postgres_delivered INTEGER NOT NULL DEFAULT 0,
                    api_delivered INTEGER NOT NULL DEFAULT 0
                )
            ''')
            # Upgrade v1.1 databases in place if needed.
            cols = {row[1] for row in conn.execute('PRAGMA table_info(events)').fetchall()}
            if 'postgres_delivered' not in cols:
                conn.execute('ALTER TABLE events ADD COLUMN postgres_delivered INTEGER NOT NULL DEFAULT 0')
            if 'api_delivered' not in cols:
                conn.execute('ALTER TABLE events ADD COLUMN api_delivered INTEGER NOT NULL DEFAULT 0')
            conn.commit()

    async def upsert_member(self, member, left_at: Optional[datetime] = None) -> None:
        await self.database.upsert_member(
            guild_id=member.guild.id,
            discord_user_id=member.id,
            username=member.name,
            display_name=member.display_name,
            is_bot=member.bot,
            joined_at=member.joined_at,
            left_at=left_at,
        )

    async def mark_member_left(self, member, left_at: datetime) -> None:
        await self.database.mark_member_left(
            guild_id=member.guild.id,
            discord_user_id=member.id,
            left_at=left_at,
        )

    async def record_event(self, event_type: str, payload: Dict[str, Any]) -> None:
        created_at_dt = datetime.now(timezone.utc)
        created_at = created_at_dt.isoformat()
        envelope = {
            'source': 'battalion-clerk',
            'event_type': event_type,
            'created_at': created_at,
            'payload': payload,
        }

        event_id = await asyncio.to_thread(self._store_local, event_type, envelope, created_at)

        postgres_delivered = False
        try:
            await self.database.record_event(event_type, payload, created_at_dt)
            if event_type == 'voice_session':
                await self.database.record_voice_session(payload)
            postgres_delivered = self.database.enabled
            if postgres_delivered:
                await asyncio.to_thread(self._mark_postgres_delivered, event_id)
        except Exception:
            log.exception('[POSTGRES ERROR] event=%s id=%s remains in local safety buffer', event_type, event_id)

        if WEBSITE_API_URL:
            delivered = await self._post_to_website(envelope)
            if delivered:
                await asyncio.to_thread(self._mark_api_delivered, event_id)
                log.info('[API DELIVERED] event=%s id=%s', event_type, event_id)

    def _store_local(self, event_type: str, envelope: Dict[str, Any], created_at: str) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                'INSERT INTO events (event_type, payload_json, created_at) VALUES (?, ?, ?)',
                (event_type, json.dumps(envelope, separators=(',', ':')), created_at),
            )
            conn.commit()
            return int(cur.lastrowid)

    def _mark_postgres_delivered(self, event_id: int) -> None:
        with self._connect() as conn:
            conn.execute('UPDATE events SET postgres_delivered = 1 WHERE id = ?', (event_id,))
            conn.commit()

    def _mark_api_delivered(self, event_id: int) -> None:
        with self._connect() as conn:
            conn.execute('UPDATE events SET api_delivered = 1 WHERE id = ?', (event_id,))
            conn.commit()

    async def _post_to_website(self, envelope: Dict[str, Any]) -> bool:
        headers = {'Content-Type': 'application/json'}
        if WEBSITE_API_KEY:
            headers['Authorization'] = f'Bearer {WEBSITE_API_KEY}'

        try:
            timeout = aiohttp.ClientTimeout(total=8)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(WEBSITE_API_URL, json=envelope, headers=headers) as response:
                    if 200 <= response.status < 300:
                        return True
                    body = await response.text()
                    log.warning('Website API returned %s: %s', response.status, body[:300])
        except Exception:
            log.exception('Unable to deliver event to website API; event remains buffered locally.')
        return False
