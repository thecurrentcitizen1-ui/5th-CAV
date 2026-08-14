import asyncio
import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

import aiohttp

log = logging.getLogger('battalion-clerk.collector')

DB_PATH = os.getenv('LOCAL_DB_PATH', 'data/battalion_clerk.db')
WEBSITE_API_URL = os.getenv('WEBSITE_API_URL', '').strip()
WEBSITE_API_KEY = os.getenv('WEBSITE_API_KEY', '').strip()


class DataCollector:
    """Stores every event locally first, then optionally forwards it to the website.

    The SQLite database is a safety buffer, not the long-term source of truth.
    On Railway, attach a Volume if you want the local buffer to survive redeploys.
    The future website database should be the authoritative persistent store.
    """

    def __init__(self) -> None:
        path = Path(DB_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = str(path)
        self._init_db()
        log.info('[BUFFER READY] SQLite=%s | website_api=%s', self.db_path, 'configured' if WEBSITE_API_URL else 'not configured')

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
                    api_delivered INTEGER NOT NULL DEFAULT 0
                )
            ''')
            conn.commit()

    async def record_event(self, event_type: str, payload: Dict[str, Any]) -> None:
        created_at = datetime.now(timezone.utc).isoformat()
        envelope = {
            'source': 'battalion-clerk',
            'event_type': event_type,
            'created_at': created_at,
            'payload': payload,
        }

        event_id = await asyncio.to_thread(self._store_local, event_type, envelope, created_at)

        if WEBSITE_API_URL:
            delivered = await self._post_to_website(envelope)
            if delivered:
                await asyncio.to_thread(self._mark_delivered, event_id)
                log.info('[API DELIVERED] event=%s id=%s', event_type, event_id)

    def _store_local(self, event_type: str, envelope: Dict[str, Any], created_at: str) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                'INSERT INTO events (event_type, payload_json, created_at) VALUES (?, ?, ?)',
                (event_type, json.dumps(envelope, separators=(',', ':')), created_at),
            )
            conn.commit()
            return int(cur.lastrowid)

    def _mark_delivered(self, event_id: int) -> None:
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
