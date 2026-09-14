"""V305 — Completed-game integrity repair for HLL: Vietnam telemetry.

Prevents Offensive objective timer extensions from being misclassified as new
matches and repairs historical false session splits so one real HLL round earns
one completed-game credit.
"""
from __future__ import annotations

import logging
import os

import hllv_rcon

log = logging.getLogger("battalion-clerk.completed-game-integrity")

_PATCH_FLAG = "_v305_completed_game_integrity_installed"
_HISTORY_DONE = False

_ORIGINAL_ENSURE_MATCH = hllv_rcon.HLLVTelemetryCollector._ensure_match
_ORIGINAL_ENSURE_SCHEMA = hllv_rcon.HLLVTelemetryCollector.ensure_schema


def _offensive_mode(server: dict) -> bool:
    return "OFFENSIVE" in str((server or {}).get("game_mode") or "").upper()


async def _guarded_ensure_match(self, server: dict):
    """Keep Offensive objective timer replenishment inside the same match.

    HLL: Vietnam Offensive replenishes/increases the remaining timer when an
    objective/sector advances. The legacy collector treated any >120-second
    increase as a same-layer round restart, which split one real game into
    multiple hll_match_sessions rows.

    A genuine same-layer rematch still splits normally because the prior timer
    is at/near zero before it resets to the new round length.
    """
    try:
        if (
            _offensive_mode(server)
            and self._active_match_id
            and self._active_match_signature
            == f"{server.get('map_id')}|{server.get('game_mode')}"
        ):
            current_remaining = int(server.get("remaining_match_time") or 0)
            current_length = int(server.get("match_length") or 0)
            previous = await self.db.fetchrow(
                "SELECT last_remaining_seconds FROM hll_match_sessions WHERE id=$1",
                self._active_match_id,
            )
            previous_remaining = int(
                (previous or {}).get("last_remaining_seconds") or 0
            )
            threshold = max(
                120, int(getattr(hllv_rcon, "RCON_POLL_SECONDS", 5) or 5) * 4
            )

            # Only suppress a timer jump when the old round was NOT near expiry.
            # Near-zero -> full timer remains a true same-layer rematch boundary.
            if (
                previous_remaining > threshold
                and current_remaining > previous_remaining + threshold
            ):
                await self.db.execute(
                    """UPDATE hll_match_sessions
                       SET last_remaining_seconds=$1,
                           last_match_length_seconds=$2,
                           last_seen_at=NOW()
                       WHERE id=$3""",
                    current_remaining,
                    current_length,
                    self._active_match_id,
                )
                log.info(
                    "[HLLV OFFENSIVE TIMER EXTENSION] match=%s previous_remaining=%s "
                    "current_remaining=%s action=CONTINUE_SAME_GAME",
                    self._active_match_id,
                    previous_remaining,
                    current_remaining,
                )
    except Exception:
        # Fail open to the original collector rather than interrupt telemetry.
        log.exception("[V305 MATCH BOUNDARY GUARD FAILED]")

    return await _ORIGINAL_ENSURE_MATCH(self, server)


async def _repair_false_offensive_splits(self) -> None:
    """Invalidate historical false Offensive split segments, reversibly.

    A false segment is a completed Offensive session whose timer was still well
    above zero and whose immediately following session on the same server is the
    same map/mode within two minutes. Those are objective timer extensions, not
    completed games.

    We retain every telemetry row. Only the false session's completion boundary
    is tombstoned by setting ended_at == started_at. Existing website progression
    queries require ended_at > started_at, so completed-game/MOS-game credit is
    corrected without deleting player service time.
    """
    global _HISTORY_DONE
    if _HISTORY_DONE or not getattr(self.db, "pool", None):
        return

    threshold = max(
        120, int(getattr(hllv_rcon, "RCON_POLL_SECONDS", 5) or 5) * 4
    )

    await self.db.execute(
        """CREATE TABLE IF NOT EXISTS hll_match_session_repairs (
               match_id BIGINT PRIMARY KEY REFERENCES hll_match_sessions(id) ON DELETE CASCADE,
               repair_code TEXT NOT NULL,
               original_ended_at TIMESTAMPTZ,
               repaired_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
           )"""
    )

    candidates = await self.db.fetch(
        """WITH ordered AS (
               SELECT
                   id, server_key, map_id, game_mode, started_at, ended_at,
                   last_remaining_seconds,
                   LEAD(id) OVER (
                       PARTITION BY COALESCE(server_key,'server_1')
                       ORDER BY started_at,id
                   ) AS next_id,
                   LEAD(map_id) OVER (
                       PARTITION BY COALESCE(server_key,'server_1')
                       ORDER BY started_at,id
                   ) AS next_map_id,
                   LEAD(game_mode) OVER (
                       PARTITION BY COALESCE(server_key,'server_1')
                       ORDER BY started_at,id
                   ) AS next_game_mode,
                   LEAD(started_at) OVER (
                       PARTITION BY COALESCE(server_key,'server_1')
                       ORDER BY started_at,id
                   ) AS next_started_at
               FROM hll_match_sessions
           )
           SELECT id, next_id, ended_at, last_remaining_seconds,
                  map_id, game_mode, next_started_at
           FROM ordered
           WHERE ended_at IS NOT NULL
             AND ended_at > started_at
             AND UPPER(COALESCE(game_mode,'')) LIKE '%OFFENSIVE%'
             AND COALESCE(last_remaining_seconds,0) > $1
             AND next_started_at IS NOT NULL
             AND ABS(EXTRACT(EPOCH FROM (next_started_at-ended_at))) <= 120
             AND COALESCE(next_map_id,'') = COALESCE(map_id,'')
             AND UPPER(COALESCE(next_game_mode,'')) = UPPER(COALESCE(game_mode,''))""",
        threshold,
    )

    ids = [int(row["id"]) for row in (candidates or [])]
    dry_run = str(os.getenv("STAGING_OFFLINE_MODE", "")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
        "enabled",
    }

    if dry_run:
        log.info(
            "[V305 COMPLETED GAME REPAIR DRY RUN] candidates=%s ids=%s",
            len(ids),
            ids[:100],
        )
        _HISTORY_DONE = True
        return

    repaired = 0
    for row in candidates or []:
        match_id = int(row["id"])
        await self.db.execute(
            """INSERT INTO hll_match_session_repairs(
                   match_id,repair_code,original_ended_at,repaired_at
               ) VALUES($1,'OFFENSIVE_TIMER_EXTENSION_SPLIT',$2,NOW())
               ON CONFLICT(match_id) DO NOTHING""",
            match_id,
            row.get("ended_at"),
        )
        await self.db.execute(
            """UPDATE hll_match_sessions
               SET ended_at=started_at,
                   result_verified_at=NULL,
                   winner_side=NULL,
                   winner_faction_id=NULL
               WHERE id=$1
                 AND ended_at IS NOT NULL
                 AND ended_at > started_at""",
            match_id,
        )
        repaired += 1

    log.info(
        "[V305 COMPLETED GAME REPAIR APPLIED] repaired=%s ids=%s",
        repaired,
        ids[:100],
    )
    _HISTORY_DONE = True


async def _schema_with_history_repair(self):
    result = await _ORIGINAL_ENSURE_SCHEMA(self)
    try:
        await _repair_false_offensive_splits(self)
    except Exception:
        # Historical cleanup must not prevent the bot from starting.
        log.exception("[V305 HISTORICAL COMPLETED GAME REPAIR FAILED]")
    return result


def install() -> bool:
    cls = hllv_rcon.HLLVTelemetryCollector
    if getattr(cls, _PATCH_FLAG, False):
        return False
    cls._ensure_match = _guarded_ensure_match
    cls.ensure_schema = _schema_with_history_repair
    setattr(cls, _PATCH_FLAG, True)
    log.info("[V305 COMPLETED GAME INTEGRITY] installed")
    return True


install()
