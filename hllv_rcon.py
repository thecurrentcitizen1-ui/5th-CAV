"""Hell Let Loose: Vietnam RCON telemetry for Battalion Clerk.

Read-only by design in the first deployment. The collector samples the live HLL:V
server and files durable match/player telemetry in PostgreSQL. RCON credentials
are read only from environment variables and are never written to the database.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

log = logging.getLogger("battalion-clerk.hllv-rcon")


def _env_bool(name: str, default: bool = False) -> bool:
    raw = str(os.getenv(name, "1" if default else "0")).strip().lower()
    return raw in {"1", "true", "yes", "on", "enabled"}


RCON_ENABLED = _env_bool("HLL_RCON_ENABLED", False)
RCON_HOST = os.getenv("HLL_RCON_HOST", "").strip()
RCON_PORT = int(os.getenv("HLL_RCON_PORT", "7779") or 7779)
RCON_PASSWORD = os.getenv("HLL_RCON_PASSWORD", "")
RCON_POLL_SECONDS = max(3, int(os.getenv("HLL_RCON_POLL_SECONDS", "5") or 5))
RCON_CM_PER_METER = max(1.0, float(os.getenv("HLL_RCON_CM_PER_METER", "100") or 100))
# Preserve helicopter movement while rejecting respawn/teleport jumps. 130 m/s
# is 468 km/h, comfortably above Vietnam-era helicopter speeds.
RCON_MAX_SPEED_MPS = max(20.0, float(os.getenv("HLL_RCON_MAX_SPEED_MPS", "130") or 130))
RCON_RECONNECT_SECONDS = max(5, int(os.getenv("HLL_RCON_RECONNECT_SECONDS", "15") or 15))


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _dump_model(value: Any) -> dict:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        try:
            return value.model_dump(mode="json", by_alias=False)
        except Exception:
            try:
                return value.model_dump()
            except Exception:
                pass
    if hasattr(value, "dict"):
        try:
            return value.dict()
        except Exception:
            pass
    try:
        return dict(vars(value))
    except Exception:
        return {}


def _first(data: dict, *names: str, default=None):
    for name in names:
        if name in data and data[name] is not None:
            return data[name]
    return default


def _json_dict(value: Any) -> dict:
    """Return a dict from PostgreSQL JSON/JSONB regardless of codec behavior.

    asyncpg can return json/jsonb columns as strings unless a custom codec is
    installed.  Accept mappings, JSON text, Pydantic models, and empty/null
    values so telemetry polling cannot fail merely because the DB codec differs.
    """
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return {}
        try:
            decoded = json.loads(raw)
            return dict(decoded) if isinstance(decoded, dict) else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
    dumped = _dump_model(value)
    return dict(dumped) if isinstance(dumped, dict) else {}


def _text(value: Any) -> str:
    if value is None:
        return ""
    if hasattr(value, "id"):
        try:
            return str(value.id)
        except Exception:
            pass
    return str(value)


def _infer_game_mode(map_id: Any) -> str:
    """Infer the human-readable HLLV mode from the layer/map id when the
    current RCON model omits gameMode.  This is based on the layer naming the
    server itself returns (warfare/offensivenva/offensiveus/domination).
    """
    raw = _text(map_id).strip().lower()
    if not raw:
        return ""
    if "offensivenva" in raw:
        return "NVA Offensive"
    if "offensiveus" in raw:
        return "US Offensive"
    if "warfare" in raw:
        return "Warfare"
    if "domination" in raw:
        return "Domination"
    if "conquest" in raw:
        return "Conquest"
    return ""


def _seconds(value: Any, default: int = 0) -> int:
    """Normalize HLL/HLLV timer values to whole seconds.

    HLLV RCON builds have returned timers both as numeric seconds and ISO-8601
    durations (for example ``PT1H30M``). Accept either representation so a
    server-side serialization change cannot stop the telemetry collector.
    """
    if value is None:
        return int(default)
    if isinstance(value, timedelta):
        return max(0, int(value.total_seconds()))
    if isinstance(value, bool):
        return int(default)
    if isinstance(value, (int, float)):
        return max(0, int(value))

    raw = str(value).strip()
    if not raw:
        return int(default)
    try:
        return max(0, int(float(raw)))
    except (TypeError, ValueError):
        pass

    # Basic ISO-8601 duration support: PnDTnHnMnS / PTnHnMnS.
    match = re.fullmatch(
        r"P(?:(?P<days>\d+(?:\.\d+)?)D)?"
        r"(?:T(?:(?P<hours>\d+(?:\.\d+)?)H)?"
        r"(?:(?P<minutes>\d+(?:\.\d+)?)M)?"
        r"(?:(?P<seconds>\d+(?:\.\d+)?)S)?)?",
        raw,
        flags=re.IGNORECASE,
    )
    if match:
        parts = {k: float(v or 0) for k, v in match.groupdict().items()}
        total = (
            parts["days"] * 86400
            + parts["hours"] * 3600
            + parts["minutes"] * 60
            + parts["seconds"]
        )
        return max(0, int(total))

    log.warning("[HLLV TIMER PARSE] unrecognized timer value=%r; using %ss", raw, default)
    return int(default)


def _position(player: Any, data: dict) -> Optional[tuple[float, float, float]]:
    pos = getattr(player, "world_position", None)
    if pos is None:
        pos = _first(data, "world_position", "worldPosition")
    if pos is None:
        return None
    if isinstance(pos, dict):
        try:
            return (float(_first(pos, "x", "X")), float(_first(pos, "y", "Y")), float(_first(pos, "z", "Z")))
        except Exception:
            return None
    if hasattr(pos, "x") and hasattr(pos, "y") and hasattr(pos, "z"):
        try:
            return (float(pos.x), float(pos.y), float(pos.z))
        except Exception:
            return None
    try:
        if len(pos) >= 3:
            return (float(pos[0]), float(pos[1]), float(pos[2]))
    except Exception:
        pass
    return None


def _nested(data: dict, *names: str) -> dict:
    value = _first(data, *names, default={})
    return value if isinstance(value, dict) else _dump_model(value)


class HLLVTelemetryCollector:
    def __init__(self, data_collector):
        self.collector = data_collector
        self.db = data_collector.db
        self.rcon = None
        self.task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self._active_match_id: Optional[int] = None
        self._active_match_signature: Optional[str] = None
        self.last_success_at: Optional[datetime] = None
        self.last_error: str = ""
        self.last_server: dict = {}
        self.last_players: int = 0

    @property
    def configured(self) -> bool:
        return bool(RCON_ENABLED and RCON_HOST and RCON_PASSWORD and RCON_PORT)

    async def ensure_schema(self):
        if not self.db.pool:
            return
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS hll_personnel_links (
                steam_id TEXT PRIMARY KEY,
                personnel_id TEXT NOT NULL,
                discord_user_id TEXT,
                hll_player_name TEXT,
                verified BOOLEAN NOT NULL DEFAULT TRUE,
                linked_by TEXT,
                linked_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await self.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_hll_personnel_links_personnel ON hll_personnel_links(personnel_id)")
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS hll_match_sessions (
                id BIGSERIAL PRIMARY KEY,
                server_name TEXT,
                map_id TEXT,
                map_name TEXT,
                game_mode TEXT,
                match_length_seconds INTEGER,
                started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                ended_at TIMESTAMPTZ,
                last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                final_allied_score INTEGER,
                final_axis_score INTEGER,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await self.db.execute("CREATE INDEX IF NOT EXISTS idx_hll_match_sessions_time ON hll_match_sessions(started_at DESC)")
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS hll_player_match_stats (
                id BIGSERIAL PRIMARY KEY,
                match_id BIGINT NOT NULL REFERENCES hll_match_sessions(id) ON DELETE CASCADE,
                steam_id TEXT NOT NULL,
                personnel_id TEXT,
                player_name TEXT,
                platform TEXT,
                team_id TEXT,
                platoon TEXT,
                platoon_index INTEGER,
                last_role_id TEXT,
                first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                connected_seconds INTEGER NOT NULL DEFAULT 0,
                distance_meters DOUBLE PRECISION NOT NULL DEFAULT 0,
                altitude_gain_meters DOUBLE PRECISION NOT NULL DEFAULT 0,
                movement_samples INTEGER NOT NULL DEFAULT 0,
                rejected_jump_samples INTEGER NOT NULL DEFAULT 0,
                role_seconds JSONB NOT NULL DEFAULT '{}'::jsonb,
                role_distance_meters JSONB NOT NULL DEFAULT '{}'::jsonb,
                role_max_speed_mps JSONB NOT NULL DEFAULT '{}'::jsonb,
                role_high_speed_seconds JSONB NOT NULL DEFAULT '{}'::jsonb,
                max_observed_speed_mps DOUBLE PRECISION NOT NULL DEFAULT 0,
                high_speed_seconds INTEGER NOT NULL DEFAULT 0,
                combat_score INTEGER NOT NULL DEFAULT 0,
                defense_score INTEGER NOT NULL DEFAULT 0,
                offense_score INTEGER NOT NULL DEFAULT 0,
                support_score INTEGER NOT NULL DEFAULT 0,
                deaths INTEGER NOT NULL DEFAULT 0,
                infantry_kills INTEGER NOT NULL DEFAULT 0,
                team_kills INTEGER NOT NULL DEFAULT 0,
                vehicle_kills INTEGER NOT NULL DEFAULT 0,
                vehicles_destroyed INTEGER NOT NULL DEFAULT 0,
                last_x DOUBLE PRECISION,
                last_y DOUBLE PRECISION,
                last_z DOUBLE PRECISION,
                last_sample_at TIMESTAMPTZ,
                last_alive BOOLEAN,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE(match_id, steam_id)
            )
        """)
        # Forward-compatible observational fields. They are deliberately not called
        # "flight time": high-speed movement is evidence for later vehicle/aviation
        # classification, not proof of aircraft occupancy.
        for ddl in (
            "ALTER TABLE hll_player_match_stats ADD COLUMN IF NOT EXISTS role_max_speed_mps JSONB NOT NULL DEFAULT '{}'::jsonb",
            "ALTER TABLE hll_player_match_stats ADD COLUMN IF NOT EXISTS role_high_speed_seconds JSONB NOT NULL DEFAULT '{}'::jsonb",
            "ALTER TABLE hll_player_match_stats ADD COLUMN IF NOT EXISTS max_observed_speed_mps DOUBLE PRECISION NOT NULL DEFAULT 0",
            "ALTER TABLE hll_player_match_stats ADD COLUMN IF NOT EXISTS high_speed_seconds INTEGER NOT NULL DEFAULT 0",
        ):
            await self.db.execute(ddl)
        await self.db.execute("CREATE INDEX IF NOT EXISTS idx_hll_player_stats_personnel ON hll_player_match_stats(personnel_id,last_seen_at DESC)")
        await self.db.execute("CREATE INDEX IF NOT EXISTS idx_hll_player_stats_steam ON hll_player_match_stats(steam_id,last_seen_at DESC)")
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS hll_rcon_health (
                id INTEGER PRIMARY KEY DEFAULT 1 CHECK(id=1),
                enabled BOOLEAN NOT NULL DEFAULT FALSE,
                connected BOOLEAN NOT NULL DEFAULT FALSE,
                host TEXT,
                port INTEGER,
                last_success_at TIMESTAMPTZ,
                last_error_at TIMESTAMPTZ,
                last_error TEXT,
                last_server_name TEXT,
                last_map_name TEXT,
                last_game_mode TEXT,
                last_player_count INTEGER NOT NULL DEFAULT 0,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await self.db.execute("""
            INSERT INTO hll_rcon_health(id,enabled,connected,host,port)
            VALUES(1,$1,FALSE,$2,$3)
            ON CONFLICT(id) DO UPDATE SET enabled=EXCLUDED.enabled,host=EXCLUDED.host,port=EXCLUDED.port,updated_at=NOW()
        """, bool(RCON_ENABLED), RCON_HOST or None, RCON_PORT)
        log.info("[HLLV RCON SCHEMA READY]")

    async def start(self):
        await self.collector.start()
        await self.ensure_schema()
        if not self.configured:
            log.warning("[HLLV RCON DISABLED] enabled=%s host=%s password=%s", RCON_ENABLED, bool(RCON_HOST), bool(RCON_PASSWORD))
            return False
        if self.task and not self.task.done():
            return True
        try:
            # HLL: Vietnam support landed in hllrcon 2.x as HLLVRcon.
            from hllrcon import HLLVRcon
        except Exception as exc:
            self.last_error = f"hllrcon import failed: {exc}"
            log.exception("[HLLV RCON IMPORT FAILED]")
            await self._health(False, self.last_error)
            return False
        self.rcon = HLLVRcon(host=RCON_HOST, port=RCON_PORT, password=RCON_PASSWORD)
        self._stop.clear()
        self.task = asyncio.create_task(self._run(), name="hllv-rcon-telemetry")
        log.info("[HLLV RCON STARTED] host=%s port=%s interval=%ss", RCON_HOST, RCON_PORT, RCON_POLL_SECONDS)
        return True

    async def stop(self):
        self._stop.set()
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except BaseException:
                pass
        self.task = None
        if self.rcon:
            try:
                self.rcon.disconnect()
            except Exception:
                pass

    async def _health(self, connected: bool, error: str = ""):
        if not self.db.pool:
            return
        now = utcnow()
        await self.db.execute("""
            UPDATE hll_rcon_health SET connected=$1,
                last_success_at=CASE WHEN $1 THEN $2 ELSE last_success_at END,
                last_error_at=CASE WHEN $1 THEN last_error_at ELSE $2 END,
                last_error=CASE WHEN $1 THEN NULL ELSE $3 END,
                last_server_name=$4,last_map_name=$5,last_game_mode=$6,last_player_count=$7,updated_at=NOW()
            WHERE id=1
        """, connected, now, error[:1000] if error else None,
             self.last_server.get("server_name"), self.last_server.get("map_name"),
             self.last_server.get("game_mode"), int(self.last_players or 0))

    async def _run(self):
        while not self._stop.is_set():
            try:
                await self.poll_once()
                self.last_success_at = utcnow()
                self.last_error = ""
                await self._health(True)
                await asyncio.sleep(RCON_POLL_SECONDS)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.warning("[HLLV RCON POLL FAILED] %s", self.last_error)
                try:
                    await self._health(False, self.last_error)
                except Exception:
                    log.exception("[HLLV RCON HEALTH WRITE FAILED]")
                try:
                    if self.rcon:
                        self.rcon.disconnect()
                except Exception:
                    pass
                await asyncio.sleep(RCON_RECONNECT_SECONDS)

    async def poll_once(self):
        if not self.rcon:
            raise RuntimeError("RCON client not initialized")
        # The client reconnects as necessary. Connection is established explicitly
        # here so failures are reflected in hll_rcon_health immediately.
        try:
            await self.rcon.connect()
        except Exception:
            # Some hllrcon builds auto-connect on first command and may consider an
            # already-connected connect() harmless/invalid. Continue to commands.
            pass
        session = await self.rcon.get_server_session()
        players_response = await self.rcon.get_players()
        server = self._server_payload(session)
        players = getattr(players_response, "players", None)
        if players is None:
            players = _first(_dump_model(players_response), "players", default=[])
        players = list(players or [])
        self.last_server = server
        self.last_players = len(players)
        match_id = await self._ensure_match(server)
        for player in players:
            await self._file_player(match_id, player)
        await self.db.execute("UPDATE hll_match_sessions SET last_seen_at=NOW() WHERE id=$1", match_id)
        log.debug("[HLLV RCON SAMPLE] match=%s map=%s players=%s", match_id, server.get("map_name"), len(players))

    def _server_payload(self, session: Any) -> dict:
        d = _dump_model(session)
        map_obj = getattr(session, "map", None)
        map_d = _dump_model(map_obj)
        layer = getattr(session, "layer", None)
        layer_d = _dump_model(layer)
        map_id = _text(_first(d, "map_id", "mapId", default=None))
        if not map_id:
            map_id = _text(_first(layer_d, "id", default=layer))
        map_name = _text(_first(d, "map_name", "mapName", default=None))
        if not map_name:
            map_name = _text(_first(map_d, "name", default=None))
        game_mode = _text(_first(d, "game_mode", "gameMode", default=None))
        if not game_mode:
            game_mode = _text(_first(layer_d, "game_mode", "gameMode", default=None))
        if not game_mode:
            game_mode = _infer_game_mode(map_id)
        return {
            "server_name": _text(_first(d, "server_name", "serverName", default="")),
            "map_id": map_id,
            "map_name": map_name,
            "game_mode": game_mode,
            "match_length": _seconds(_first(d, "match_length", "matchLength", default=0)),
            "remaining_match_time": _seconds(_first(d, "remaining_match_time", "remainingMatchTime", default=0)),
            "allied_score": int(_first(d, "allied_score", "alliedScore", default=0) or 0),
            "axis_score": int(_first(d, "axis_score", "axisScore", default=0) or 0),
        }

    async def _ensure_match(self, server: dict) -> int:
        signature = f"{server.get('map_id')}|{server.get('game_mode')}"
        # Map/mode changes are authoritative round boundaries. A match timer reset
        # on the same layer is also detected by closing records that have been stale.
        if self._active_match_id and self._active_match_signature == signature:
            return self._active_match_id
        if self._active_match_id:
            await self.db.execute("""
                UPDATE hll_match_sessions SET ended_at=COALESCE(ended_at,NOW()),
                    final_allied_score=$1,final_axis_score=$2,last_seen_at=NOW()
                WHERE id=$3
            """, int(server.get("allied_score") or 0), int(server.get("axis_score") or 0), self._active_match_id)
        row = await self.db.fetchrow("""
            INSERT INTO hll_match_sessions(server_name,map_id,map_name,game_mode,match_length_seconds)
            VALUES($1,$2,$3,$4,$5) RETURNING id
        """, server.get("server_name"), server.get("map_id"), server.get("map_name"), server.get("game_mode"), int(server.get("match_length") or 0))
        self._active_match_id = int(row["id"])
        self._active_match_signature = signature
        log.info("[HLLV MATCH OPEN] id=%s map=%s mode=%s", self._active_match_id, server.get("map_name"), server.get("game_mode"))
        return self._active_match_id

    async def _personnel_id(self, steam_id: str) -> Optional[str]:
        row = await self.db.fetchrow("SELECT personnel_id FROM hll_personnel_links WHERE steam_id=$1 AND verified=TRUE", steam_id)
        return str(row["personnel_id"]) if row else None

    async def _file_player(self, match_id: int, player: Any):
        d = _dump_model(player)
        steam_id = _text(_first(d, "steam_id", "steamId", "steamID", default=""))
        if not steam_id:
            # Some HLLV models call this platform-specific identifier simply id.
            candidate = _text(_first(d, "id", "iD", default=""))
            # The web RCON response exposes both iD and steamId. Only treat long
            # numeric IDs as SteamID64 when the dedicated field is absent.
            if candidate.isdigit() and len(candidate) >= 16:
                steam_id = candidate
        if not steam_id:
            return
        personnel_id = await self._personnel_id(steam_id)
        name = _text(_first(d, "name", default=""))
        platform = _text(_first(d, "platform", default=""))
        team_id = _text(_first(d, "team_id", "teamId", "team", default=""))
        platoon = _text(_first(d, "platoon", default=""))
        try:
            platoon_index = int(_first(d, "platoon_index", "platoonIndex", default=0) or 0)
        except Exception:
            platoon_index = 0
        role = getattr(player, "role", None)
        role_id = _text(_first(d, "role_id", "roleId", "role", default=role))
        score = _nested(d, "score_data", "scoreData")
        stats = _nested(d, "stats")
        pos = _position(player, d)
        alive = bool(pos and not all(abs(v) < 0.001 for v in pos))
        now = utcnow()
        row = await self.db.fetchrow("SELECT * FROM hll_player_match_stats WHERE match_id=$1 AND steam_id=$2", match_id, steam_id)
        if not row:
            x, y, z = pos if pos else (None, None, None)
            await self.db.execute("""
                INSERT INTO hll_player_match_stats(
                    match_id,steam_id,personnel_id,player_name,platform,team_id,platoon,platoon_index,last_role_id,
                    combat_score,defense_score,offense_score,support_score,deaths,infantry_kills,team_kills,vehicle_kills,vehicles_destroyed,
                    last_x,last_y,last_z,last_sample_at,last_alive
                ) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21,$22,$23)
            """, match_id, steam_id, personnel_id, name, platform, team_id, platoon, platoon_index, role_id,
                 int(_first(score, "combat", "COMBAT", default=0) or 0), int(_first(score, "defense", "DEFENSE", default=0) or 0),
                 int(_first(score, "offense", "OFFENSE", default=0) or 0), int(_first(score, "support", "SUPPORT", default=0) or 0),
                 int(_first(stats, "deaths", default=0) or 0), int(_first(stats, "infantry_kills", "infantryKills", default=0) or 0),
                 int(_first(stats, "team_kills", "teamKills", default=0) or 0), int(_first(stats, "vehicle_kills", "vehicleKills", default=0) or 0),
                 int(_first(stats, "vehicles_destroyed", "vehiclesDestroyed", default=0) or 0), x, y, z, now, alive)
            if personnel_id:
                await self.db.execute("UPDATE hll_personnel_links SET hll_player_name=$1,updated_at=NOW() WHERE steam_id=$2", name, steam_id)
            return

        last_sample = row.get("last_sample_at")
        dt = max(0.0, (now - last_sample).total_seconds()) if last_sample else 0.0
        # Do not accrue absurd time after outages; this is active sampled presence.
        accrue_seconds = int(min(dt, RCON_POLL_SECONDS * 3)) if dt > 0 else 0
        distance_m = 0.0
        altitude_gain_m = 0.0
        observed_speed_mps = 0.0
        rejected = 0
        if pos and alive and row.get("last_alive") and row.get("last_x") is not None and dt > 0:
            raw = math.sqrt((pos[0]-float(row["last_x"]))**2 + (pos[1]-float(row["last_y"]))**2 + (pos[2]-float(row["last_z"]))**2)
            candidate_m = raw / RCON_CM_PER_METER
            speed = candidate_m / dt
            if speed <= RCON_MAX_SPEED_MPS:
                observed_speed_mps = max(0.0, speed)
                distance_m = candidate_m
                dz_m = (pos[2] - float(row["last_z"])) / RCON_CM_PER_METER
                altitude_gain_m = max(0.0, dz_m)
            else:
                rejected = 1
        role_seconds = _json_dict(row.get("role_seconds"))
        role_distance = _json_dict(row.get("role_distance_meters"))
        role_max_speed = _json_dict(row.get("role_max_speed_mps"))
        role_high_speed = _json_dict(row.get("role_high_speed_seconds"))
        role_key = role_id or "UNKNOWN"
        role_seconds[role_key] = int(role_seconds.get(role_key, 0) or 0) + accrue_seconds
        role_distance[role_key] = float(role_distance.get(role_key, 0.0) or 0.0) + distance_m
        role_max_speed[role_key] = max(float(role_max_speed.get(role_key, 0.0) or 0.0), observed_speed_mps)
        # 15 m/s (~54 km/h) is intentionally only an observation threshold.
        # It can represent vehicles or aircraft and is never treated as flight time.
        high_speed_add = accrue_seconds if observed_speed_mps >= 15.0 else 0
        role_high_speed[role_key] = int(role_high_speed.get(role_key, 0) or 0) + high_speed_add
        x, y, z = pos if pos else (None, None, None)
        await self.db.execute("""
            UPDATE hll_player_match_stats SET
                personnel_id=COALESCE($1,personnel_id),player_name=$2,platform=$3,team_id=$4,platoon=$5,platoon_index=$6,last_role_id=$7,
                last_seen_at=$8,connected_seconds=connected_seconds+$9,distance_meters=distance_meters+$10,
                altitude_gain_meters=altitude_gain_meters+$11,movement_samples=movement_samples+$12,rejected_jump_samples=rejected_jump_samples+$13,
                role_seconds=$14::jsonb,role_distance_meters=$15::jsonb,
                role_max_speed_mps=$16::jsonb,role_high_speed_seconds=$17::jsonb,
                max_observed_speed_mps=GREATEST(COALESCE(max_observed_speed_mps,0),$18),
                high_speed_seconds=COALESCE(high_speed_seconds,0)+$19,
                combat_score=$20,defense_score=$21,offense_score=$22,support_score=$23,deaths=$24,infantry_kills=$25,team_kills=$26,vehicle_kills=$27,vehicles_destroyed=$28,
                last_x=$29,last_y=$30,last_z=$31,last_sample_at=$8,last_alive=$32,updated_at=NOW()
            WHERE id=$33
        """, personnel_id, name, platform, team_id, platoon, platoon_index, role_id, now, accrue_seconds, distance_m,
             altitude_gain_m, 1 if distance_m > 0 else 0, rejected, json.dumps(role_seconds), json.dumps(role_distance),
             json.dumps(role_max_speed), json.dumps(role_high_speed), observed_speed_mps, high_speed_add,
             int(_first(score, "combat", "COMBAT", default=0) or 0), int(_first(score, "defense", "DEFENSE", default=0) or 0),
             int(_first(score, "offense", "OFFENSE", default=0) or 0), int(_first(score, "support", "SUPPORT", default=0) or 0),
             int(_first(stats, "deaths", default=0) or 0), int(_first(stats, "infantry_kills", "infantryKills", default=0) or 0),
             int(_first(stats, "team_kills", "teamKills", default=0) or 0), int(_first(stats, "vehicle_kills", "vehicleKills", default=0) or 0),
             int(_first(stats, "vehicles_destroyed", "vehiclesDestroyed", default=0) or 0), x, y, z, alive, row["id"])
        if personnel_id:
            await self.db.execute("UPDATE hll_personnel_links SET hll_player_name=$1,updated_at=NOW() WHERE steam_id=$2", name, steam_id)

    async def status(self) -> dict:
        if not self.db.pool:
            return {"configured": self.configured, "connected": False, "error": "DATABASE_URL unavailable"}
        row = await self.db.fetchrow("SELECT * FROM hll_rcon_health WHERE id=1")
        return {
            "configured": self.configured,
            "connected": bool(row and row.get("connected")),
            "last_success_at": row.get("last_success_at") if row else None,
            "last_error": (row.get("last_error") if row else None) or self.last_error,
            "server_name": row.get("last_server_name") if row else None,
            "map_name": row.get("last_map_name") if row else None,
            "game_mode": row.get("last_game_mode") if row else None,
            "player_count": int(row.get("last_player_count") or 0) if row else 0,
        }

    async def link_personnel(self, guild_id: int, discord_user_id: int, steam_id: str, linked_by: str) -> dict:
        await self.collector.start()
        steam_id = str(steam_id or "").strip()
        if not (steam_id.isdigit() and len(steam_id) == 17):
            return {"ok": False, "error": "SteamID64 must be exactly 17 digits."}
        person = await self.db.fetchrow("""
            SELECT p.id::text AS personnel_id,p.rank_code,p.first_name,p.last_name
            FROM personnel p
            JOIN website_member_links w ON w.personnel_id=p.id::text
            WHERE w.guild_id::text=$1 AND w.discord_user_id::text=$2
            LIMIT 1
        """, str(guild_id), str(discord_user_id))
        if not person:
            return {"ok": False, "error": "No active Soldier Record is linked to this Discord account."}
        try:
            await self.db.execute("""
                INSERT INTO hll_personnel_links(steam_id,personnel_id,discord_user_id,linked_by,verified,updated_at)
                VALUES($1,$2,$3,$4,TRUE,NOW())
                ON CONFLICT(steam_id) DO UPDATE SET personnel_id=EXCLUDED.personnel_id,discord_user_id=EXCLUDED.discord_user_id,
                    linked_by=EXCLUDED.linked_by,verified=TRUE,updated_at=NOW()
            """, steam_id, person["personnel_id"], str(discord_user_id), linked_by)
        except Exception as exc:
            # Unique personnel mapping means one Soldier cannot silently own two Steam IDs.
            return {"ok": False, "error": f"Link conflict: {exc}"}
        name = f"{person.get('rank_code') or ''} {person.get('first_name') or ''} {person.get('last_name') or ''}".strip()
        return {"ok": True, "personnel_id": person["personnel_id"], "soldier": name, "steam_id": steam_id}

    async def unlink_personnel(self, guild_id: int, discord_user_id: int) -> bool:
        person = await self.db.fetchrow("""
            SELECT p.id::text AS personnel_id FROM personnel p JOIN website_member_links w ON w.personnel_id=p.id::text
            WHERE w.guild_id::text=$1 AND w.discord_user_id::text=$2 LIMIT 1
        """, str(guild_id), str(discord_user_id))
        if not person:
            return False
        result = await self.db.execute("DELETE FROM hll_personnel_links WHERE personnel_id=$1", person["personnel_id"])
        return bool(result and not str(result).endswith(" 0"))

    async def personnel_stats(self, guild_id: int, discord_user_id: int) -> Optional[dict]:
        person = await self.db.fetchrow("""
            SELECT p.id::text AS personnel_id,p.rank_code,p.first_name,p.last_name FROM personnel p
            JOIN website_member_links w ON w.personnel_id=p.id::text
            WHERE w.guild_id::text=$1 AND w.discord_user_id::text=$2 LIMIT 1
        """, str(guild_id), str(discord_user_id))
        if not person:
            return None
        link = await self.db.fetchrow("SELECT * FROM hll_personnel_links WHERE personnel_id=$1", person["personnel_id"])
        agg = await self.db.fetchrow("""
            SELECT COUNT(DISTINCT match_id) AS matches,
                   COALESCE(SUM(connected_seconds),0)::bigint AS seconds,
                   COALESCE(SUM(distance_meters),0)::double precision AS distance,
                   COALESCE(SUM(altitude_gain_meters),0)::double precision AS altitude_gain,
                   COALESCE(SUM(infantry_kills),0)::bigint AS infantry_kills,
                   COALESCE(SUM(vehicle_kills),0)::bigint AS vehicle_kills,
                   COALESCE(SUM(vehicles_destroyed),0)::bigint AS vehicles_destroyed,
                   COALESCE(SUM(deaths),0)::bigint AS deaths,
                   COALESCE(SUM(combat_score),0)::bigint AS combat_score,
                   COALESCE(SUM(defense_score),0)::bigint AS defense_score,
                   COALESCE(SUM(offense_score),0)::bigint AS offense_score,
                   COALESCE(SUM(support_score),0)::bigint AS support_score
            FROM hll_player_match_stats WHERE personnel_id=$1
        """, person["personnel_id"])
        latest = await self.db.fetchrow("""
            SELECT s.*,m.map_name,m.game_mode,m.started_at FROM hll_player_match_stats s
            JOIN hll_match_sessions m ON m.id=s.match_id WHERE s.personnel_id=$1 ORDER BY s.last_seen_at DESC LIMIT 1
        """, person["personnel_id"])
        aggregate=dict(agg) if agg else {}
        hours=float(aggregate.get("seconds") or 0)/3600.0
        matches=int(aggregate.get("matches") or 0)
        if hours >= 40 or matches >= 25: field_experience="VETERAN"
        elif hours >= 15 or matches >= 10: field_experience="COMBAT TESTED"
        elif hours >= 5 or matches >= 3: field_experience="FIELD EXPERIENCED"
        else: field_experience="NEWLY ARRIVED"
        aggregate["field_experience"]=field_experience
        aggregate["score_total"]=sum(int(aggregate.get(k) or 0) for k in ("combat_score","defense_score","offense_score","support_score"))
        return {"person": dict(person), "link": dict(link) if link else None, "aggregate": aggregate, "latest": dict(latest) if latest else None}
