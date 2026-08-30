"""Hell Let Loose: Vietnam RCON telemetry for Battalion Clerk.

Read-only by design in the first deployment. The collector samples the live HLL:V
server and files durable match/player telemetry in PostgreSQL. RCON credentials
are read only from environment variables and are never written to the database.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import re
import warnings
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

log = logging.getLogger("battalion-clerk.hllv-rcon")


def _env_bool(name: str, default: bool = False) -> bool:
    raw = str(os.getenv(name, "1" if default else "0")).strip().lower()
    return raw in {"1", "true", "yes", "on", "enabled"}


RCON_ENABLED = _env_bool("HLL_RCON_ENABLED", False)
RCON_HOST = os.getenv("HLL_RCON_HOST", "").strip()
RCON_PORT = int(os.getenv("HLL_RCON_PORT", "7779") or 7779)
RCON_PASSWORD = os.getenv("HLL_RCON_PASSWORD", "")
RCON_POLL_SECONDS = max(3, int(os.getenv("HLL_RCON_POLL_SECONDS", "5") or 5))
SEEDING_TIMEZONE = ZoneInfo("America/New_York")
SEEDING_STOP_PLAYERS = max(1, int(os.getenv("HLL_SEED_READY_PLAYERS", "40") or 40))
RCON_CM_PER_METER = max(1.0, float(os.getenv("HLL_RCON_CM_PER_METER", "100") or 100))
# Preserve helicopter movement while rejecting respawn/teleport jumps. 130 m/s
# is 468 km/h, comfortably above Vietnam-era helicopter speeds.
RCON_MAX_SPEED_MPS = max(20.0, float(os.getenv("HLL_RCON_MAX_SPEED_MPS", "130") or 130))
RCON_RECONNECT_SECONDS = max(5, int(os.getenv("HLL_RCON_RECONNECT_SECONDS", "15") or 15))

# Single, low-frequency 1/5 CAV server recruiting broadcast.
RCON_RECRUITING_WEBSITE = "WWW.5THCAVGAMING.COM"
RCON_RECRUITING_INTERVAL_SECONDS = 30 * 60
RCON_RECRUITING_DISPLAY_SECONDS = 20
RCON_RECRUITING_MESSAGE = (
    "WELCOME TO THE 5TH CAVALRY SERVER\n\n"
    "Charlie won’t wait, so why should you.\n\n"
    f"ENLIST TODAY - {RCON_RECRUITING_WEBSITE.lower()}"
)
# Conservative airborne-movement signature. A verified Pilot role files this as
# Airmobile Flight Time; every other role files it as Slick Ride time. This does
# not claim direct seat telemetry (HLLV RCON does not expose vehicle occupancy).
RCON_AIRMOBILE_MIN_SPEED_MPS = max(15.0, float(os.getenv("HLL_RCON_AIRMOBILE_MIN_SPEED_MPS", "20") or 20))


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _dump_model(value: Any) -> dict:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        # Current HLLV builds sometimes return ISO-8601 duration strings in a
        # field typed as timedelta by the upstream client. Pydantic can still
        # serialize the model, but emits one warning per 5-second poll. Suppress
        # only those serialization warnings here; the collector normalizes the
        # actual value through _seconds() before filing it.
        try:
            with warnings.catch_warnings():
                warnings.filterwarnings(
                    "ignore",
                    message=r".*serialized value may not be as expected.*",
                    category=UserWarning,
                    module=r"pydantic.*",
                )
                try:
                    return value.model_dump(mode="json", by_alias=False, warnings=False)
                except TypeError:
                    return value.model_dump(mode="json", by_alias=False)
        except Exception:
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", UserWarning)
                    try:
                        return value.model_dump(warnings=False)
                    except TypeError:
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


def _player_identity(data: dict) -> tuple[str, str, str, str]:
    """Return (player_key, steam_id64, platform_id, eos_id).

    Existing database columns retain the historical ``steam_id`` name for
    compatibility, but player_key may be a SteamID64, console platform ID, or
    EOS ID. Display names are never used as the durable identity key.
    """
    steam_id = _text(_first(data, "steam_id", "steamId", "steamID", default="")).strip()
    eos_id = _text(_first(data, "eos_id", "eosId", "eosID", "epic_online_services_id", "epicOnlineServicesId", default="")).strip()
    platform_id = _text(_first(data, "platform_id", "platformId", "platform_user_id", "platformUserId", default="")).strip()
    raw_id = _text(_first(data, "id", "iD", default="")).strip()
    if not platform_id and raw_id and raw_id != steam_id:
        platform_id = raw_id
    player_key = steam_id or eos_id or platform_id
    return player_key, steam_id, platform_id, eos_id


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


def _looks_like_m16(value: Any) -> bool:
    raw = _text(value).upper().replace("-", "").replace(" ", "")
    return any(token in raw for token in ("M16", "XM16"))


def _weapon_label(value: Any) -> str:
    if value is None:
        return ""
    for attr in ("name", "display_name", "id", "weapon_id"):
        candidate = getattr(value, attr, None)
        if candidate:
            return str(candidate)
    return _text(value)


HLL_KNOWN_ROLE_MAPPINGS = {
    # Confirmed role IDs are also mapped to the community battlefield MOS so
    # verified role_seconds can drive MOS proficiency without a second timer.
    "0": {"name": "RIFLEMAN", "category": "INFANTRY", "mos_code": "11R"},
    "3": {"name": "MEDIC", "category": "MEDICAL", "mos_code": "91M"},
    "5": {"name": "SPECIALIST", "category": "INFANTRY", "mos_code": ""},
    "6": {"name": "MACHINE GUNNER", "category": "INFANTRY", "mos_code": "11M"},
    "7": {"name": "GRENADIER", "category": "INFANTRY", "mos_code": "11G"},
    "8": {"name": "ENGINEER", "category": "SUPPORT", "mos_code": "12E"},
    "9": {"name": "SQUAD LEADER", "category": "LEADERSHIP", "mos_code": "11L"},
    "10": {"name": "SNIPER", "category": "INFANTRY", "mos_code": ""},
    "11": {"name": "CREWMAN", "category": "ARMOR", "mos_code": "19K"},
    "12": {"name": "TANK COMMANDER", "category": "ARMOR", "mos_code": "19C"},
    "16": {"name": "PILOT", "category": "AVIATION", "mos_code": "67P"},
    "17": {"name": "LOGISTICS OFFICER", "category": "AVIATION", "mos_code": "67L"},
    "20": {"name": "COMMANDER", "category": "LEADERSHIP", "mos_code": ""},
}


def _role_label(role: Any, data: dict) -> str:
    """Best-effort human label without trusting it as authoritative.

    HLLV RCON V2 guarantees an integer Role field. Some hllrcon builds enrich
    that integer into a generated enum/model. Preserve any readable label we
    can observe, but keep role_id as the durable authoritative key until staff
    verifies the mapping in the Telemetry Lab.
    """
    for candidate in (
        _first(data, "role_name", "roleName", "role_label", "roleLabel"),
        getattr(role, "name", None), getattr(role, "display_name", None),
        getattr(role, "label", None),
    ):
        if candidate:
            label=str(candidate).replace("HLLVRole.", "").replace("_", " ").strip()
            # Numeric enum names are not useful member-facing labels. Prefer a
            # confirmed mapping when one is known.
            if not label.isdigit():
                return label
    role_id=_text(_first(data, "role_id", "roleId", "role", default=role))
    return HLL_KNOWN_ROLE_MAPPINGS.get(role_id,{}).get("name","")


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
        self._broadcast_match_id: Optional[int] = None
        self._broadcast_next_elapsed: Optional[int] = None
        self._broadcast_generation: int = 0
        self._broadcast_local_match_started: Optional[datetime] = None

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
                platform TEXT,
                platform_user_id TEXT,
                eos_id TEXT,
                verified BOOLEAN NOT NULL DEFAULT TRUE,
                linked_by TEXT,
                linked_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await self.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_hll_personnel_links_personnel ON hll_personnel_links(personnel_id)")
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS hll_identity_claims (
                id BIGSERIAL PRIMARY KEY,
                recruiting_case_id UUID,
                personnel_id TEXT NOT NULL,
                discord_user_id TEXT,
                platform TEXT NOT NULL,
                claimed_identity TEXT NOT NULL,
                normalized_identity TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'PENDING',
                linked_player_key TEXT,
                error TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                linked_at TIMESTAMPTZ
            )
        """)
        await self.db.execute("CREATE INDEX IF NOT EXISTS idx_hll_identity_claims_pending ON hll_identity_claims(status,platform,created_at)")
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
                platform_user_id TEXT,
                eos_id TEXT,
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
                role_airmobile_seconds JSONB NOT NULL DEFAULT '{}'::jsonb,
                role_airmobile_distance_meters JSONB NOT NULL DEFAULT '{}'::jsonb,
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
            "ALTER TABLE hll_personnel_links ADD COLUMN IF NOT EXISTS platform TEXT",
            "ALTER TABLE hll_personnel_links ADD COLUMN IF NOT EXISTS platform_user_id TEXT",
            "ALTER TABLE hll_personnel_links ADD COLUMN IF NOT EXISTS eos_id TEXT",
            "ALTER TABLE hll_player_match_stats ADD COLUMN IF NOT EXISTS platform_user_id TEXT",
            "ALTER TABLE hll_player_match_stats ADD COLUMN IF NOT EXISTS eos_id TEXT",
            "ALTER TABLE hll_player_match_stats ADD COLUMN IF NOT EXISTS role_max_speed_mps JSONB NOT NULL DEFAULT '{}'::jsonb",
            "ALTER TABLE hll_player_match_stats ADD COLUMN IF NOT EXISTS role_high_speed_seconds JSONB NOT NULL DEFAULT '{}'::jsonb",
            "ALTER TABLE hll_player_match_stats ADD COLUMN IF NOT EXISTS max_observed_speed_mps DOUBLE PRECISION NOT NULL DEFAULT 0",
            "ALTER TABLE hll_player_match_stats ADD COLUMN IF NOT EXISTS high_speed_seconds INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE hll_player_match_stats ADD COLUMN IF NOT EXISTS role_airmobile_seconds JSONB NOT NULL DEFAULT '{}'::jsonb",
            "ALTER TABLE hll_player_match_stats ADD COLUMN IF NOT EXISTS role_airmobile_distance_meters JSONB NOT NULL DEFAULT '{}'::jsonb",
        ):
            await self.db.execute(ddl)
        await self.db.execute("CREATE INDEX IF NOT EXISTS idx_hll_player_stats_personnel ON hll_player_match_stats(personnel_id,last_seen_at DESC)")
        await self.db.execute("CREATE INDEX IF NOT EXISTS idx_hll_player_stats_steam ON hll_player_match_stats(steam_id,last_seen_at DESC)")
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS hll_seeding_service (
                personnel_id UUID NOT NULL REFERENCES personnel(id) ON DELETE CASCADE,
                service_date DATE NOT NULL,
                credited_seconds INTEGER NOT NULL DEFAULT 0,
                first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY(personnel_id,service_date)
            )
        """)
        await self.db.execute("CREATE INDEX IF NOT EXISTS idx_hll_seeding_service_person ON hll_seeding_service(personnel_id,service_date DESC)")
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS hll_role_mappings (
                role_id TEXT PRIMARY KEY,
                observed_label TEXT,
                verified_role_name TEXT,
                role_category TEXT,
                mos_code TEXT,
                verified BOOLEAN NOT NULL DEFAULT FALSE,
                verified_by TEXT,
                verified_at TIMESTAMPTZ,
                notes TEXT,
                first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                sample_count BIGINT NOT NULL DEFAULT 0
            )
        """)
        for role_id, role_info in HLL_KNOWN_ROLE_MAPPINGS.items():
            await self.db.execute("""
                INSERT INTO hll_role_mappings(role_id,observed_label,verified_role_name,role_category,mos_code,verified,verified_by,verified_at,notes,last_seen_at)
                VALUES($1,$2,$2,$3,$4,TRUE,'ROLE-ID-MAP',NOW(),'Confirmed HLL: Vietnam role mapping supplied by unit staff.',NOW())
                ON CONFLICT(role_id) DO UPDATE SET
                    observed_label=EXCLUDED.observed_label,
                    verified_role_name=EXCLUDED.verified_role_name,
                    role_category=EXCLUDED.role_category,
                    mos_code=EXCLUDED.mos_code,
                    verified=TRUE,
                    verified_by='ROLE-ID-MAP',
                    verified_at=COALESCE(hll_role_mappings.verified_at,NOW()),
                    notes=EXCLUDED.notes,
                    last_seen_at=NOW()
            """, role_id, role_info["name"], role_info["category"], role_info["mos_code"])

        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS hll_role_loadout_observations (
                role_id TEXT NOT NULL,
                loadout TEXT NOT NULL DEFAULT '',
                observed_label TEXT,
                first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                sample_count BIGINT NOT NULL DEFAULT 0,
                max_speed_mps DOUBLE PRECISION NOT NULL DEFAULT 0,
                max_vertical_speed_mps DOUBLE PRECISION NOT NULL DEFAULT 0,
                high_speed_seconds BIGINT NOT NULL DEFAULT 0,
                altitude_gain_meters DOUBLE PRECISION NOT NULL DEFAULT 0,
                infantry_kills_delta BIGINT NOT NULL DEFAULT 0,
                vehicle_kills_delta BIGINT NOT NULL DEFAULT 0,
                PRIMARY KEY(role_id,loadout)
            )
        """)
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS hll_research_samples (
                id BIGSERIAL PRIMARY KEY,
                match_id BIGINT REFERENCES hll_match_sessions(id) ON DELETE CASCADE,
                steam_id TEXT NOT NULL,
                personnel_id TEXT,
                observed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                role_id TEXT,
                observed_role_label TEXT,
                loadout TEXT,
                team_id TEXT,
                platoon TEXT,
                x DOUBLE PRECISION,y DOUBLE PRECISION,z DOUBLE PRECISION,
                speed_mps DOUBLE PRECISION NOT NULL DEFAULT 0,
                vertical_speed_mps DOUBLE PRECISION NOT NULL DEFAULT 0,
                connected_delta_seconds INTEGER NOT NULL DEFAULT 0,
                infantry_kills INTEGER NOT NULL DEFAULT 0,
                deaths INTEGER NOT NULL DEFAULT 0,
                vehicle_kills INTEGER NOT NULL DEFAULT 0,
                vehicles_destroyed INTEGER NOT NULL DEFAULT 0,
                combat_score INTEGER NOT NULL DEFAULT 0,
                defense_score INTEGER NOT NULL DEFAULT 0,
                offense_score INTEGER NOT NULL DEFAULT 0,
                support_score INTEGER NOT NULL DEFAULT 0
            )
        """)
        await self.db.execute("CREATE INDEX IF NOT EXISTS idx_hll_research_personnel_time ON hll_research_samples(personnel_id,observed_at DESC)")
        await self.db.execute("CREATE INDEX IF NOT EXISTS idx_hll_research_role_time ON hll_research_samples(role_id,observed_at DESC)")
        # Exact weapon-attribution events from the HLLV admin log.  These are
        # separate from estimated ammunition expenditure: a KILL / TEAM KILL log
        # proves weapon use for that event, but the game does not expose shots fired.
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS hll_weapon_events (
                id BIGSERIAL PRIMARY KEY,
                event_key TEXT UNIQUE NOT NULL,
                match_id BIGINT REFERENCES hll_match_sessions(id) ON DELETE SET NULL,
                event_at TIMESTAMPTZ NOT NULL,
                event_type TEXT NOT NULL,
                attacker_id TEXT,
                personnel_id TEXT,
                attacker_name TEXT,
                victim_id TEXT,
                victim_name TEXT,
                weapon_id TEXT,
                weapon_name TEXT,
                is_m16 BOOLEAN NOT NULL DEFAULT FALSE,
                raw_message TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await self.db.execute("CREATE INDEX IF NOT EXISTS idx_hll_weapon_events_personnel_time ON hll_weapon_events(personnel_id,event_at DESC)")
        await self.db.execute("CREATE INDEX IF NOT EXISTS idx_hll_weapon_events_attacker_time ON hll_weapon_events(attacker_id,event_at DESC)")
        await self.db.execute("CREATE INDEX IF NOT EXISTS idx_hll_weapon_events_m16 ON hll_weapon_events(is_m16,event_at DESC)")
        for ddl in (
            "ALTER TABLE hll_player_match_stats ADD COLUMN IF NOT EXISTS m16_carried_seconds INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE hll_player_match_stats ADD COLUMN IF NOT EXISTS m16_distance_meters DOUBLE PRECISION NOT NULL DEFAULT 0"
        ):
            await self.db.execute(ddl)
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
        # Low-frequency recruiting broadcast is isolated from telemetry. A failed
        # message can never interrupt player stats, weapon logs, or service records.
        try:
            await self._run_recruiting_broadcast(match_id, server, players)
        except Exception as exc:
            log.warning("[HLLV RECRUITING BROADCAST FAILED] %s: %s", type(exc).__name__, exc)
        seeding_now = self._is_seeding_credit_window(len(players))
        seeding_credits=[]
        for player in players:
            filed=await self._file_player(match_id, player)
            if seeding_now and filed and filed[0] and int(filed[1] or 0)>0:
                seeding_credits.append((filed[0],int(filed[1] or 0)))
        if seeding_credits:
            await self._file_seeding_credit(seeding_credits)
        try:
            await self._reconcile_pending_identity_claims()
        except Exception as exc:
            log.warning("[HLLV IDENTITY AUTO-LINK FAILED] %s: %s", type(exc).__name__, exc)
        # Weapon attribution comes from the authoritative HLLV admin log.  Pull a
        # short overlapping window every poll and deduplicate by deterministic key.
        # A temporary log failure must not stop player telemetry collection.
        try:
            await self._poll_weapon_logs(match_id)
        except Exception as exc:
            log.warning("[HLLV WEAPON LOG POLL FAILED] %s: %s", type(exc).__name__, exc)
        await self.db.execute("UPDATE hll_match_sessions SET last_seen_at=NOW() WHERE id=$1", match_id)
        log.debug("[HLLV RCON SAMPLE] match=%s map=%s players=%s", match_id, server.get("map_name"), len(players))

    def _is_seeding_credit_window(self, player_count: int) -> bool:
        """Credit only the configured nightly seeding period while population is below 40."""
        now_et=utcnow().astimezone(SEEDING_TIMEZONE)
        minutes=now_et.hour*60+now_et.minute
        return 20*60 <= minutes <= 21*60+30 and int(player_count or 0) < SEEDING_STOP_PLAYERS

    async def _file_seeding_credit(self, credits: list[tuple[str,int]]):
        now_et=utcnow().astimezone(SEEDING_TIMEZONE)
        service_date=now_et.date()
        combined={}
        for pid,seconds in credits:
            if pid and seconds>0:
                combined[str(pid)]=combined.get(str(pid),0)+int(seconds)
        for pid,seconds in combined.items():
            await self.db.execute("""
                INSERT INTO hll_seeding_service(personnel_id,service_date,credited_seconds,first_seen_at,last_seen_at)
                VALUES($1::uuid,$2,$3,NOW(),NOW())
                ON CONFLICT(personnel_id,service_date) DO UPDATE SET
                  credited_seconds=hll_seeding_service.credited_seconds+EXCLUDED.credited_seconds,
                  last_seen_at=NOW()
            """,pid,service_date,seconds)

    def _round_elapsed_seconds(self, server: dict) -> int:
        """Best-effort elapsed round time for the 30-minute broadcast clock."""
        total = int(server.get("match_length") or 0)
        remaining = int(server.get("remaining_match_time") or 0)
        if total > 0 and 0 <= remaining <= total:
            return max(0, total - remaining)
        if self._broadcast_local_match_started:
            return max(0, int((utcnow() - self._broadcast_local_match_started).total_seconds()))
        return 0

    async def _run_recruiting_broadcast(self, match_id: int, server: dict, players: list[Any]):
        """Broadcast one recruiting hook every 30 minutes for 20 seconds.

        No permanent welcome text, private player messages, or rotating ads are used.
        The first poll in an already-running round schedules only the *next*
        30-minute mark so a restart never dumps missed advertisements on players.
        """
        if not self.rcon or not players:
            return

        if self._broadcast_match_id != match_id:
            self._broadcast_match_id = match_id
            self._broadcast_generation += 1
            self._broadcast_local_match_started = utcnow()
            elapsed = self._round_elapsed_seconds(server)
            interval = RCON_RECRUITING_INTERVAL_SECONDS
            self._broadcast_next_elapsed = max(interval, ((elapsed // interval) + 1) * interval)
            return

        elapsed = self._round_elapsed_seconds(server)
        next_due = self._broadcast_next_elapsed or RCON_RECRUITING_INTERVAL_SECONDS
        if elapsed < next_due:
            return

        # Advance first so transient errors or timer jitter cannot repeatedly spam.
        interval = RCON_RECRUITING_INTERVAL_SECONDS
        self._broadcast_next_elapsed = ((elapsed // interval) + 1) * interval
        await self._send_recruiting_broadcast()

    async def send_manual_broadcast(self, message: str, display_seconds: int = 10) -> dict:
        """Send one staff-requested global server broadcast, then clear it.

        This intentionally does not alter the recurring recruiting clock.  The
        shared broadcast generation prevents an older scheduled clear task from
        erasing a newer manual message (or vice versa).
        """
        text = " ".join(str(message or "").split()).strip()
        if not text:
            return {"ok": False, "error": "Message cannot be blank."}
        if len(text) > 180:
            return {"ok": False, "error": "Message is too long. Keep it to 180 characters or fewer."}
        if not self.configured:
            return {"ok": False, "error": "HLL: Vietnam server connection is not configured."}
        if not self.rcon:
            return {"ok": False, "error": "HLL: Vietnam server connection is not currently available."}

        seconds = max(3, min(int(display_seconds or 10), 30))
        self._broadcast_generation += 1
        generation = self._broadcast_generation
        try:
            await self.rcon.broadcast(text)
        except Exception as exc:
            log.warning("[HLLV MANUAL BROADCAST FAILED] %s: %s", type(exc).__name__, exc)
            return {"ok": False, "error": f"Server broadcast failed: {type(exc).__name__}"}

        log.info("[HLLV MANUAL BROADCAST] duration=%ss chars=%s", seconds, len(text))

        async def clear_later():
            try:
                await asyncio.sleep(seconds)
                if self.rcon and generation == self._broadcast_generation:
                    await self.rcon.broadcast("")
                    log.info("[HLLV MANUAL BROADCAST CLEARED]")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("[HLLV MANUAL BROADCAST CLEAR FAILED] %s: %s", type(exc).__name__, exc)

        asyncio.create_task(clear_later(), name="hllv-manual-broadcast-clear")
        return {"ok": True, "message": text, "display_seconds": seconds}

    async def clear_manual_broadcast(self) -> dict:
        """Immediately clear the current global server broadcast."""
        if not self.rcon:
            return {"ok": False, "error": "HLL: Vietnam server connection is not currently available."}
        self._broadcast_generation += 1
        try:
            await self.rcon.broadcast("")
            log.info("[HLLV MANUAL BROADCAST CLEARED BY STAFF]")
            return {"ok": True}
        except Exception as exc:
            log.warning("[HLLV MANUAL BROADCAST CLEAR FAILED] %s: %s", type(exc).__name__, exc)
            return {"ok": False, "error": f"Server broadcast clear failed: {type(exc).__name__}"}

    async def _send_recruiting_broadcast(self):
        self._broadcast_generation += 1
        generation = self._broadcast_generation
        await self.rcon.broadcast(RCON_RECRUITING_MESSAGE)
        log.info("[HLLV RECRUITING BROADCAST] duration=%ss website=%s",
                 RCON_RECRUITING_DISPLAY_SECONDS, RCON_RECRUITING_WEBSITE)

        async def clear_later():
            try:
                await asyncio.sleep(RCON_RECRUITING_DISPLAY_SECONDS)
                if self.rcon and generation == self._broadcast_generation:
                    await self.rcon.broadcast("")
                    log.info("[HLLV RECRUITING BROADCAST CLEARED]")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("[HLLV RECRUITING CLEAR FAILED] %s: %s", type(exc).__name__, exc)

        asyncio.create_task(clear_later(), name="hllv-recruiting-broadcast-clear")

    async def _poll_weapon_logs(self, match_id: int):
        # RCON V2 GetAdminLog uses seconds, not minutes.  Keep a small overlap so
        # events near poll boundaries are not missed; event_key makes repeats safe.
        response = await self.rcon.get_admin_log(max(20, RCON_POLL_SECONDS * 4))
        entries = getattr(response, "entries", None)
        if entries is None:
            entries = _first(_dump_model(response), "entries", default=[])
        for entry in list(entries or []):
            await self._file_weapon_log(match_id, entry)

    async def _file_weapon_log(self, match_id: int, entry: Any):
        d = _dump_model(entry)
        class_name = type(entry).__name__.upper()
        raw = _text(getattr(entry, "raw_message", None) or _first(d, "raw_message", "rawMessage", "message", default=""))
        event_type = ""
        if "TEAMKILL" in class_name or raw.upper().startswith("TEAM KILL:"):
            event_type = "BLUE_ON_BLUE"
        elif "KILL" in class_name or raw.upper().startswith("KILL:"):
            event_type = "KILL"
        if not event_type:
            return

        attacker_id = _text(getattr(entry, "instigator_id", None) or _first(d, "instigator_id", "instigatorId", default=""))
        attacker_name = _text(getattr(entry, "instigator_name", None) or _first(d, "instigator_name", "instigatorName", default=""))
        victim_id = _text(getattr(entry, "victim_id", None) or _first(d, "victim_id", "victimId", default=""))
        victim_name = _text(getattr(entry, "victim_name", None) or _first(d, "victim_name", "victimName", default=""))
        weapon_id = _text(getattr(entry, "weapon_id", None) or _first(d, "weapon_id", "weaponId", default=""))
        if raw and not attacker_id:
            match = re.match(
                r"^(?:TEAM KILL|KILL): (?P<attacker>.+)\((?:Allies|Axis)/(?P<attacker_id>\d{17}|[\da-f]{32})\) -> "
                r"(?P<victim>.+)\((?:Allies|Axis)/(?P<victim_id>\d{17}|[\da-f]{32})\) with (?P<weapon>.+)$",
                raw, flags=re.IGNORECASE
            )
            if match:
                attacker_name = match.group("attacker")
                attacker_id = match.group("attacker_id")
                victim_name = match.group("victim")
                victim_id = match.group("victim_id")
                weapon_id = match.group("weapon")
        weapon_name = weapon_id
        try:
            getter = getattr(entry, "get_weapon", None)
            if callable(getter):
                weapon_name = _weapon_label(getter()) or weapon_id
        except Exception:
            pass

        ts = getattr(entry, "timestamp", None) or _first(d, "timestamp", default=None) or utcnow()
        if not isinstance(ts, datetime):
            try:
                ts = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            except Exception:
                ts = utcnow()
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        personnel_id = await self._personnel_id(attacker_id) if attacker_id else None
        key_material = "|".join((str(int(ts.timestamp())), event_type, attacker_id, victim_id, weapon_id, attacker_name, victim_name))
        event_key = hashlib.sha1(key_material.encode("utf-8", "ignore")).hexdigest()
        is_m16 = _looks_like_m16(weapon_id) or _looks_like_m16(weapon_name)
        await self.db.execute("""
            INSERT INTO hll_weapon_events(event_key,match_id,event_at,event_type,attacker_id,personnel_id,attacker_name,victim_id,victim_name,weapon_id,weapon_name,is_m16,raw_message)
            VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
            ON CONFLICT(event_key) DO UPDATE SET
              match_id=COALESCE(hll_weapon_events.match_id,EXCLUDED.match_id),
              personnel_id=COALESCE(hll_weapon_events.personnel_id,EXCLUDED.personnel_id),
              weapon_name=COALESCE(NULLIF(EXCLUDED.weapon_name,''),hll_weapon_events.weapon_name),
              is_m16=hll_weapon_events.is_m16 OR EXCLUDED.is_m16
        """, event_key, match_id, ts, event_type, attacker_id or None, personnel_id, attacker_name or None, victim_id or None, victim_name or None, weapon_id or None, weapon_name or None, is_m16, raw or None)

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
        player_key, steam_id64, platform_user_id, eos_id = _player_identity(d)
        if not player_key:
            return
        # The historical column name is steam_id, but it now stores the durable
        # HLL player key for Steam, Xbox, and PlayStation identities.
        steam_id = player_key
        personnel_id = await self._personnel_id(player_key)
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
        role_label = _role_label(role, d)
        loadout = _text(_first(d, "loadout", default=""))
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
                    match_id,steam_id,personnel_id,player_name,platform,platform_user_id,eos_id,team_id,platoon,platoon_index,last_role_id,
                    combat_score,defense_score,offense_score,support_score,deaths,infantry_kills,team_kills,vehicle_kills,vehicles_destroyed,
                    last_x,last_y,last_z,last_sample_at,last_alive
                ) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21,$22,$23,$24,$25)
            """, match_id, steam_id, personnel_id, name, platform, platform_user_id or None, eos_id or None, team_id, platoon, platoon_index, role_id,
                 int(_first(score, "combat", "COMBAT", default=0) or 0), int(_first(score, "defense", "DEFENSE", default=0) or 0),
                 int(_first(score, "offense", "OFFENSE", default=0) or 0), int(_first(score, "support", "SUPPORT", default=0) or 0),
                 int(_first(stats, "deaths", default=0) or 0), int(_first(stats, "infantry_kills", "infantryKills", default=0) or 0),
                 int(_first(stats, "team_kills", "teamKills", default=0) or 0), int(_first(stats, "vehicle_kills", "vehicleKills", default=0) or 0),
                 int(_first(stats, "vehicles_destroyed", "vehiclesDestroyed", default=0) or 0), x, y, z, now, alive)
            if personnel_id:
                await self.db.execute("UPDATE hll_personnel_links SET hll_player_name=$1,platform=COALESCE(NULLIF($2,''),platform),platform_user_id=COALESCE(NULLIF($3,''),platform_user_id),eos_id=COALESCE(NULLIF($4,''),eos_id),updated_at=NOW() WHERE steam_id=$5", name, platform, platform_user_id, eos_id, steam_id)
            return (personnel_id,0)

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
        vertical_speed_mps = 0.0
        if pos and row.get("last_z") is not None and dt > 0 and observed_speed_mps > 0:
            vertical_speed_mps = abs((pos[2] - float(row["last_z"])) / RCON_CM_PER_METER / dt)
        cur_inf_kills = int(_first(stats, "infantry_kills", "infantryKills", default=0) or 0)
        cur_vehicle_kills = int(_first(stats, "vehicle_kills", "vehicleKills", default=0) or 0)
        inf_kill_delta = max(0, cur_inf_kills - int(row.get("infantry_kills") or 0))
        vehicle_kill_delta = max(0, cur_vehicle_kills - int(row.get("vehicle_kills") or 0))
        role_seconds = _json_dict(row.get("role_seconds"))
        role_distance = _json_dict(row.get("role_distance_meters"))
        role_max_speed = _json_dict(row.get("role_max_speed_mps"))
        role_high_speed = _json_dict(row.get("role_high_speed_seconds"))
        role_airmobile_seconds = _json_dict(row.get("role_airmobile_seconds"))
        role_airmobile_distance = _json_dict(row.get("role_airmobile_distance_meters"))
        role_key = role_id or "UNKNOWN"
        role_seconds[role_key] = int(role_seconds.get(role_key, 0) or 0) + accrue_seconds
        role_distance[role_key] = float(role_distance.get(role_key, 0.0) or 0.0) + distance_m
        role_max_speed[role_key] = max(float(role_max_speed.get(role_key, 0.0) or 0.0), observed_speed_mps)
        # 15 m/s (~54 km/h) is intentionally only an observation threshold.
        # It can represent vehicles or aircraft and is never treated as flight time.
        high_speed_add = accrue_seconds if observed_speed_mps >= 15.0 else 0
        role_high_speed[role_key] = int(role_high_speed.get(role_key, 0) or 0) + high_speed_add
        # Air Cav ledger: movement above the conservative aircraft-signature
        # threshold is filed by role. Website classification gives Role 16 Pilot
        # and Role 17 Logistics Officer flight credit; every other role remains a
        # Slick Ride (passenger/gunner/crew/infantry/etc.).
        airmobile_add = accrue_seconds if observed_speed_mps >= RCON_AIRMOBILE_MIN_SPEED_MPS else 0
        airmobile_distance_add = distance_m if airmobile_add else 0.0
        role_airmobile_seconds[role_key] = int(role_airmobile_seconds.get(role_key, 0) or 0) + airmobile_add
        role_airmobile_distance[role_key] = float(role_airmobile_distance.get(role_key, 0.0) or 0.0) + airmobile_distance_add
        x, y, z = pos if pos else (None, None, None)
        # Research registry: observe every role/loadout combination, but only retain
        # high-frequency position samples for linked 1/5 CAV Soldiers. This keeps
        # the database useful without creating millions of rows for public players.
        await self.db.execute("""
            INSERT INTO hll_role_mappings(role_id,observed_label,last_seen_at,sample_count)
            VALUES($1,$2,NOW(),1) ON CONFLICT(role_id) DO UPDATE SET
              observed_label=COALESCE(NULLIF(EXCLUDED.observed_label,''),hll_role_mappings.observed_label),
              last_seen_at=NOW(),sample_count=hll_role_mappings.sample_count+1
        """, role_key, role_label or None)
        await self.db.execute("""
            INSERT INTO hll_role_loadout_observations(role_id,loadout,observed_label,last_seen_at,sample_count,max_speed_mps,max_vertical_speed_mps,high_speed_seconds,altitude_gain_meters,infantry_kills_delta,vehicle_kills_delta)
            VALUES($1,$2,$3,NOW(),1,$4,$5,$6,$7,$8,$9)
            ON CONFLICT(role_id,loadout) DO UPDATE SET
              observed_label=COALESCE(NULLIF(EXCLUDED.observed_label,''),hll_role_loadout_observations.observed_label),
              last_seen_at=NOW(),sample_count=hll_role_loadout_observations.sample_count+1,
              max_speed_mps=GREATEST(hll_role_loadout_observations.max_speed_mps,EXCLUDED.max_speed_mps),
              max_vertical_speed_mps=GREATEST(hll_role_loadout_observations.max_vertical_speed_mps,EXCLUDED.max_vertical_speed_mps),
              high_speed_seconds=hll_role_loadout_observations.high_speed_seconds+EXCLUDED.high_speed_seconds,
              altitude_gain_meters=hll_role_loadout_observations.altitude_gain_meters+EXCLUDED.altitude_gain_meters,
              infantry_kills_delta=hll_role_loadout_observations.infantry_kills_delta+EXCLUDED.infantry_kills_delta,
              vehicle_kills_delta=hll_role_loadout_observations.vehicle_kills_delta+EXCLUDED.vehicle_kills_delta
        """, role_key, loadout, role_label or None, observed_speed_mps, vertical_speed_mps, high_speed_add, altitude_gain_m, inf_kill_delta, vehicle_kill_delta)
        if personnel_id:
            await self.db.execute("""
                INSERT INTO hll_research_samples(match_id,steam_id,personnel_id,observed_at,role_id,observed_role_label,loadout,team_id,platoon,x,y,z,speed_mps,vertical_speed_mps,connected_delta_seconds,infantry_kills,deaths,vehicle_kills,vehicles_destroyed,combat_score,defense_score,offense_score,support_score)
                VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21,$22,$23)
            """, match_id, steam_id, personnel_id, now, role_key, role_label or None, loadout, team_id, platoon, x, y, z, observed_speed_mps, vertical_speed_mps, accrue_seconds,
                 cur_inf_kills, int(_first(stats, "deaths", default=0) or 0), cur_vehicle_kills, int(_first(stats, "vehicles_destroyed", "vehiclesDestroyed", default=0) or 0),
                 int(_first(score, "combat", "COMBAT", default=0) or 0), int(_first(score, "defense", "DEFENSE", default=0) or 0), int(_first(score, "offense", "OFFENSE", default=0) or 0), int(_first(score, "support", "SUPPORT", default=0) or 0))
        await self.db.execute("""
            UPDATE hll_player_match_stats SET
                personnel_id=COALESCE($1,personnel_id),player_name=$2,platform=$3,team_id=$4,platoon=$5,platoon_index=$6,last_role_id=$7,
                last_seen_at=$8,connected_seconds=connected_seconds+$9,distance_meters=distance_meters+$10,
                altitude_gain_meters=altitude_gain_meters+$11,movement_samples=movement_samples+$12,rejected_jump_samples=rejected_jump_samples+$13,
                role_seconds=$14::jsonb,role_distance_meters=$15::jsonb,
                role_max_speed_mps=$16::jsonb,role_high_speed_seconds=$17::jsonb,
                role_airmobile_seconds=$18::jsonb,role_airmobile_distance_meters=$19::jsonb,
                max_observed_speed_mps=GREATEST(COALESCE(max_observed_speed_mps,0),$20),
                high_speed_seconds=COALESCE(high_speed_seconds,0)+$21,
                m16_carried_seconds=COALESCE(m16_carried_seconds,0)+$22,
                m16_distance_meters=COALESCE(m16_distance_meters,0)+$23,
                combat_score=$24,defense_score=$25,offense_score=$26,support_score=$27,deaths=$28,infantry_kills=$29,team_kills=$30,vehicle_kills=$31,vehicles_destroyed=$32,
                last_x=$33,last_y=$34,last_z=$35,last_sample_at=$8,last_alive=$36,updated_at=NOW()
            WHERE id=$37
        """, personnel_id, name, platform, team_id, platoon, platoon_index, role_id, now, accrue_seconds, distance_m,
             altitude_gain_m, 1 if distance_m > 0 else 0, rejected, json.dumps(role_seconds), json.dumps(role_distance),
             json.dumps(role_max_speed), json.dumps(role_high_speed), json.dumps(role_airmobile_seconds), json.dumps(role_airmobile_distance),
             observed_speed_mps, high_speed_add,
             accrue_seconds if personnel_id else 0, distance_m if personnel_id else 0.0,
             int(_first(score, "combat", "COMBAT", default=0) or 0), int(_first(score, "defense", "DEFENSE", default=0) or 0),
             int(_first(score, "offense", "OFFENSE", default=0) or 0), int(_first(score, "support", "SUPPORT", default=0) or 0),
             int(_first(stats, "deaths", default=0) or 0), int(_first(stats, "infantry_kills", "infantryKills", default=0) or 0),
             int(_first(stats, "team_kills", "teamKills", default=0) or 0), int(_first(stats, "vehicle_kills", "vehicleKills", default=0) or 0),
             int(_first(stats, "vehicles_destroyed", "vehiclesDestroyed", default=0) or 0), x, y, z, alive, row["id"])
        if personnel_id:
            await self.db.execute("UPDATE hll_personnel_links SET hll_player_name=$1,platform=COALESCE(NULLIF($2,''),platform),platform_user_id=COALESCE(NULLIF($3,''),platform_user_id),eos_id=COALESCE(NULLIF($4,''),eos_id),updated_at=NOW() WHERE steam_id=$5", name, platform, platform_user_id, eos_id, steam_id)
        return (personnel_id,accrue_seconds)


    async def _vip_call(self, names, *args):
        """Call the installed hllrcon VIP primitive without hard-coding one library patch.

        HLL RCON V2 defines Add VIP, Remove VIP, Get VIPs and Set VIP Slot Count.
        hllrcon releases have used slightly different Python method names, so this
        probes only known semantic variants and fails closed when none are exposed.
        """
        if not self.rcon:
            raise RuntimeError("HLL RCON is not connected")
        for name in names:
            fn=getattr(self.rcon,name,None)
            if callable(fn):
                return await fn(*args)
        raise RuntimeError("Installed hllrcon build does not expose the required VIP command")

    async def get_vip_ids(self) -> set[str]:
        response=await self._vip_call(("get_vip_ids","get_vips","get_vip_players"))
        data=_dump_model(response)
        values=[]
        if isinstance(response,(list,tuple,set)):
            values=list(response)
        elif data:
            values=_first(data,"players","vips","vip_ids","vipIds","ids",default=[]) or []
        out=set()
        for value in values:
            item=_dump_model(value)
            pid=_text(_first(item,"player_id","playerId","id","steam_id","steamId",default=value if isinstance(value,str) else "")).strip()
            if pid: out.add(pid)
        return out

    async def add_vip(self, player_id: str, comment: str = "1/5 CAV"):
        player_id=str(player_id or "").strip()
        if not player_id: raise ValueError("player_id required")
        return await self._vip_call(("add_vip","vip_add"),player_id,str(comment or "1/5 CAV")[:120])

    async def remove_vip(self, player_id: str):
        player_id=str(player_id or "").strip()
        if not player_id: raise ValueError("player_id required")
        return await self._vip_call(("remove_vip","vip_remove","delete_vip"),player_id)

    async def set_vip_slot_count(self, count: int):
        count=max(0,int(count))
        return await self._vip_call(("set_vip_slot_count","set_vip_slots","set_vip_slots_num"),count)

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

    async def _reconcile_pending_identity_claims(self):
        """Resolve recruiting-filed HLL identities without requiring a Discord command."""
        claims = await self.db.fetch("""
            SELECT * FROM hll_identity_claims
            WHERE status='PENDING'
            ORDER BY created_at ASC
            LIMIT 100
        """)
        for claim in claims:
            platform=str(claim.get("platform") or "").upper()
            identity=str(claim.get("claimed_identity") or "").strip()
            personnel_id=str(claim.get("personnel_id") or "").strip()
            discord_user_id=str(claim.get("discord_user_id") or "").strip() or None
            if not personnel_id or not identity:
                continue
            try:
                if platform == "STEAM":
                    if not (identity.isdigit() and len(identity) == 17):
                        await self.db.execute("UPDATE hll_identity_claims SET status='ERROR',error=$1,updated_at=NOW() WHERE id=$2", "Invalid SteamID64", claim["id"])
                        continue
                    conflict = await self.db.fetchrow("SELECT personnel_id FROM hll_personnel_links WHERE steam_id=$1", identity)
                    if conflict and str(conflict.get("personnel_id") or "") != personnel_id:
                        await self.db.execute("UPDATE hll_identity_claims SET status='CONFLICT',error=$1,updated_at=NOW() WHERE id=$2", "SteamID64 already linked to another Soldier", claim["id"])
                        continue
                    owned = await self.db.fetchrow("SELECT steam_id FROM hll_personnel_links WHERE personnel_id=$1", personnel_id)
                    if owned and str(owned.get("steam_id") or "") != identity:
                        await self.db.execute("UPDATE hll_identity_claims SET status='CONFLICT',error=$1,updated_at=NOW() WHERE id=$2", "Soldier already linked to a different HLL identity", claim["id"])
                        continue
                    await self.db.execute("""
                        INSERT INTO hll_personnel_links(steam_id,personnel_id,discord_user_id,platform,platform_user_id,linked_by,verified,updated_at)
                        VALUES($1,$2,$3,'STEAM',$1,'RECRUITING APPROVAL',TRUE,NOW())
                        ON CONFLICT(steam_id) DO UPDATE SET personnel_id=EXCLUDED.personnel_id,discord_user_id=EXCLUDED.discord_user_id,
                          platform='STEAM',platform_user_id=EXCLUDED.platform_user_id,linked_by='RECRUITING APPROVAL',verified=TRUE,updated_at=NOW()
                    """, identity, personnel_id, discord_user_id)
                    await self.db.execute("UPDATE hll_identity_claims SET status='VERIFIED',linked_player_key=$1,error=NULL,linked_at=NOW(),updated_at=NOW() WHERE id=$2", identity, claim["id"])
                    try:
                        await self.db.execute("UPDATE recruiting_cases SET game_identity_link_status='VERIFIED',game_identity_link_error=NULL,game_identity_linked_at=NOW(),updated_at=NOW() WHERE id=$1", claim.get("recruiting_case_id"))
                    except Exception:
                        pass
                    continue

                if platform not in {"XBOX", "PS5"}:
                    continue
                if platform == "XBOX":
                    pred = "LOWER(COALESCE(platform,'')) LIKE '%xbox%'"
                else:
                    pred = "(LOWER(COALESCE(platform,'')) LIKE '%playstation%' OR LOWER(COALESCE(platform,'')) LIKE '%ps5%' OR LOWER(COALESCE(platform,'')) LIKE '%psn%')"
                row = await self.db.fetchrow(f"""
                    SELECT steam_id,player_name,platform,platform_user_id,eos_id,last_seen_at
                    FROM hll_player_match_stats
                    WHERE LOWER(player_name)=LOWER($1) AND {pred}
                    ORDER BY last_seen_at DESC LIMIT 1
                """, identity)
                if not row:
                    continue
                player_key=str(row.get("steam_id") or "").strip()
                if not player_key:
                    continue
                conflict=await self.db.fetchrow("SELECT personnel_id FROM hll_personnel_links WHERE steam_id=$1",player_key)
                if conflict and str(conflict.get("personnel_id") or "") != personnel_id:
                    await self.db.execute("UPDATE hll_identity_claims SET status='CONFLICT',error=$1,updated_at=NOW() WHERE id=$2", "Observed console account already linked to another Soldier", claim["id"])
                    continue
                owned=await self.db.fetchrow("SELECT steam_id FROM hll_personnel_links WHERE personnel_id=$1",personnel_id)
                if owned and str(owned.get("steam_id") or "") != player_key:
                    await self.db.execute("UPDATE hll_identity_claims SET status='CONFLICT',error=$1,updated_at=NOW() WHERE id=$2", "Soldier already linked to a different HLL identity", claim["id"])
                    continue
                await self.db.execute("""
                    INSERT INTO hll_personnel_links(steam_id,personnel_id,discord_user_id,hll_player_name,platform,platform_user_id,eos_id,linked_by,verified,updated_at)
                    VALUES($1,$2,$3,$4,$5,$6,$7,'RECRUITING AUTO-VERIFY',TRUE,NOW())
                    ON CONFLICT(steam_id) DO UPDATE SET personnel_id=EXCLUDED.personnel_id,discord_user_id=EXCLUDED.discord_user_id,
                      hll_player_name=EXCLUDED.hll_player_name,platform=EXCLUDED.platform,platform_user_id=EXCLUDED.platform_user_id,eos_id=EXCLUDED.eos_id,
                      linked_by='RECRUITING AUTO-VERIFY',verified=TRUE,updated_at=NOW()
                """, player_key, personnel_id, discord_user_id, row.get("player_name"), row.get("platform"), row.get("platform_user_id"), row.get("eos_id"))
                await self.db.execute("UPDATE hll_player_match_stats SET personnel_id=$1 WHERE steam_id=$2",personnel_id,player_key)
                await self.db.execute("UPDATE hll_research_samples SET personnel_id=$1 WHERE steam_id=$2",personnel_id,player_key)
                await self.db.execute("UPDATE hll_identity_claims SET status='VERIFIED',linked_player_key=$1,error=NULL,linked_at=NOW(),updated_at=NOW() WHERE id=$2",player_key,claim["id"])
                try:
                    await self.db.execute("UPDATE recruiting_cases SET game_identity_link_status='VERIFIED',game_identity_link_error=NULL,game_identity_linked_at=NOW(),updated_at=NOW() WHERE id=$1", claim.get("recruiting_case_id"))
                except Exception:
                    pass
            except Exception as exc:
                log.warning("[HLLV IDENTITY CLAIM] id=%s platform=%s error=%s", claim.get("id"), platform, exc)
                await self.db.execute("UPDATE hll_identity_claims SET error=$1,updated_at=NOW() WHERE id=$2", str(exc)[:500], claim["id"])

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
            # Backfill any telemetry collected before the Soldier linked their account.
            await self.db.execute("UPDATE hll_player_match_stats SET personnel_id=$1 WHERE steam_id=$2 AND (personnel_id IS NULL OR personnel_id=$1)", person["personnel_id"], steam_id)
            try:
                await self.db.execute("UPDATE hll_research_samples SET personnel_id=$1 WHERE steam_id=$2 AND (personnel_id IS NULL OR personnel_id=$1)", person["personnel_id"], steam_id)
            except Exception:
                pass
        except Exception as exc:
            # Unique personnel mapping means one Soldier cannot silently own two Steam IDs.
            return {"ok": False, "error": f"Link conflict: {exc}"}
        name = f"{person.get('rank_code') or ''} {person.get('first_name') or ''} {person.get('last_name') or ''}".strip()
        return {"ok": True, "personnel_id": person["personnel_id"], "soldier": name, "steam_id": steam_id}

    async def link_console_personnel(self, guild_id: int, discord_user_id: int, platform: str, gamertag: str, linked_by: str) -> dict:
        """Resolve an Xbox/PlayStation display name to the durable RCON identity
        already observed on this server, then link it to a Soldier Record.
        """
        await self.collector.start()
        platform = str(platform or "").strip().upper()
        gamertag = str(gamertag or "").strip()
        if platform not in {"XBOX", "PS5"}:
            return {"ok": False, "error": "Platform must be Xbox or PlayStation 5."}
        if not gamertag:
            return {"ok": False, "error": "Enter the console gamertag / PSN Online ID."}
        person = await self.db.fetchrow("""
            SELECT p.id::text AS personnel_id,p.rank_code,p.first_name,p.last_name
            FROM personnel p JOIN website_member_links w ON w.personnel_id=p.id::text
            WHERE w.guild_id::text=$1 AND w.discord_user_id::text=$2 LIMIT 1
        """, str(guild_id), str(discord_user_id))
        if not person:
            return {"ok": False, "error": "No active Soldier Record is linked to that Discord account."}
        # HLLV platform labels vary slightly by RCON/client build. Match the
        # requested console family plus the exact visible in-game name.
        if platform == "XBOX":
            platform_pred = "LOWER(COALESCE(platform,'')) LIKE '%xbox%'"
        else:
            platform_pred = "(LOWER(COALESCE(platform,'')) LIKE '%playstation%' OR LOWER(COALESCE(platform,'')) LIKE '%ps5%' OR LOWER(COALESCE(platform,'')) LIKE '%psn%')"
        row = await self.db.fetchrow(f"""
            SELECT steam_id,player_name,platform,platform_user_id,eos_id,last_seen_at
            FROM hll_player_match_stats
            WHERE LOWER(player_name)=LOWER($1) AND {platform_pred}
            ORDER BY last_seen_at DESC LIMIT 1
        """, gamertag)
        if not row:
            return {"ok": False, "error": f"No {platform} player named '{gamertag}' has been observed by Battalion Clerk yet. Have them join the 1/5 CAV server once, then run this command again."}
        player_key = str(row.get("steam_id") or "").strip()
        if not player_key:
            return {"ok": False, "error": "The server saw that player name but did not expose a stable platform identity. Try again while the player is currently in the server."}
        try:
            await self.db.execute("""
                INSERT INTO hll_personnel_links(steam_id,personnel_id,discord_user_id,hll_player_name,platform,platform_user_id,eos_id,linked_by,verified,updated_at)
                VALUES($1,$2,$3,$4,$5,$6,$7,$8,TRUE,NOW())
                ON CONFLICT(steam_id) DO UPDATE SET personnel_id=EXCLUDED.personnel_id,discord_user_id=EXCLUDED.discord_user_id,
                    hll_player_name=EXCLUDED.hll_player_name,platform=EXCLUDED.platform,platform_user_id=EXCLUDED.platform_user_id,eos_id=EXCLUDED.eos_id,
                    linked_by=EXCLUDED.linked_by,verified=TRUE,updated_at=NOW()
            """, player_key, person["personnel_id"], str(discord_user_id), row.get("player_name"), row.get("platform"), row.get("platform_user_id"), row.get("eos_id"), linked_by)
            await self.db.execute("UPDATE hll_player_match_stats SET personnel_id=$1 WHERE steam_id=$2", person["personnel_id"], player_key)
            await self.db.execute("UPDATE hll_research_samples SET personnel_id=$1 WHERE steam_id=$2", person["personnel_id"], player_key)
        except Exception as exc:
            return {"ok": False, "error": f"Link conflict: {exc}"}
        name = f"{person.get('rank_code') or ''} {person.get('first_name') or ''} {person.get('last_name') or ''}".strip()
        return {"ok": True, "status":"VERIFIED", "verified":True, "personnel_id": person["personnel_id"], "soldier": name, "player_name": row.get("player_name"), "platform": platform, "player_key": player_key}

    async def staff_link_identity(self, guild_id: int, discord_user_id: int, platform: str, identity: str, linked_by: str) -> dict:
        """Command-staff repair path for HLL identity links.

        Steam links are verified immediately after validation. Console names are
        verified immediately when Battalion Clerk has already observed the exact
        player on the unit server; otherwise a pending identity claim is filed
        and the normal telemetry reconciler will verify it automatically on the
        player's next appearance.
        """
        await self.collector.start()
        platform = str(platform or "").strip().upper()
        identity = str(identity or "").strip()
        if platform not in {"STEAM", "XBOX", "PS5"}:
            return {"ok": False, "error": "Platform must be Steam, Xbox, or PlayStation 5."}
        if not identity:
            return {"ok": False, "error": "Enter a SteamID64, Xbox Gamertag, or PSN Online ID."}
        if platform == "STEAM":
            result = await self.link_personnel(guild_id, discord_user_id, identity, linked_by)
            if result.get("ok"):
                result["status"] = "VERIFIED"
                result["platform"] = "STEAM"
                result["identity"] = result.get("steam_id")
            return result

        # First try an immediate console resolution against identities already
        # observed by RCON. This preserves the existing durable-key logic.
        result = await self.link_console_personnel(guild_id, discord_user_id, platform, identity, linked_by)
        if result.get("ok"):
            result["status"] = "VERIFIED"
            result["identity"] = result.get("player_name") or identity
            return result

        error = str(result.get("error") or "")
        if "has been observed by Battalion Clerk yet" not in error:
            return result

        # The Soldier is valid but the console account has not appeared on the
        # server yet. File the same pending claim used by recruiting approval so
        # staff do not need to ask the member to run a command later.
        person = await self.db.fetchrow("""
            SELECT p.id::text AS personnel_id,p.rank_code,p.first_name,p.last_name
            FROM personnel p
            JOIN website_member_links w ON w.personnel_id=p.id::text
            WHERE w.guild_id::text=$1 AND w.discord_user_id::text=$2
            LIMIT 1
        """, str(guild_id), str(discord_user_id))
        if not person:
            return {"ok": False, "error": "No active Soldier Record is linked to that Discord account."}

        personnel_id = str(person["personnel_id"])
        normalized = identity.casefold().strip()
        # Do not allow a second live claim to silently replace a different one.
        existing = await self.db.fetchrow("""
            SELECT id,platform,claimed_identity,status
            FROM hll_identity_claims
            WHERE personnel_id=$1 AND status='PENDING'
            ORDER BY created_at DESC LIMIT 1
        """, personnel_id)
        if existing:
            same = (str(existing.get("platform") or "").upper() == platform and
                    str(existing.get("claimed_identity") or "").casefold().strip() == normalized)
            if same:
                name = f"{person.get('rank_code') or ''} {person.get('first_name') or ''} {person.get('last_name') or ''}".strip()
                return {"ok": True, "status": "PENDING", "personnel_id": personnel_id,
                        "soldier": name, "platform": platform, "identity": identity,
                        "claim_id": existing.get("id")}
            await self.db.execute("UPDATE hll_identity_claims SET status='SUPERSEDED',updated_at=NOW() WHERE id=$1", existing.get("id"))

        # Refuse a pending console identity already claimed for somebody else.
        conflict = await self.db.fetchrow("""
            SELECT personnel_id FROM hll_identity_claims
            WHERE platform=$1 AND normalized_identity=$2 AND status='PENDING'
              AND personnel_id<>$3
            ORDER BY created_at DESC LIMIT 1
        """, platform, normalized, personnel_id)
        if conflict:
            return {"ok": False, "error": "That console identity already has a pending link to another Soldier."}

        claim = await self.db.fetchrow("""
            INSERT INTO hll_identity_claims(
                recruiting_case_id,personnel_id,discord_user_id,platform,claimed_identity,
                normalized_identity,status,error,created_at,updated_at
            ) VALUES(NULL,$1,$2,$3,$4,$5,'PENDING',NULL,NOW(),NOW())
            RETURNING id
        """, personnel_id, str(discord_user_id), platform, identity, normalized)
        name = f"{person.get('rank_code') or ''} {person.get('first_name') or ''} {person.get('last_name') or ''}".strip()
        return {"ok": True, "status": "PENDING", "personnel_id": personnel_id,
                "soldier": name, "platform": platform, "identity": identity,
                "claim_id": claim.get("id") if claim else None}

    async def unlink_personnel(self, guild_id: int, discord_user_id: int) -> bool:
        person = await self.db.fetchrow("""
            SELECT p.id::text AS personnel_id FROM personnel p JOIN website_member_links w ON w.personnel_id=p.id::text
            WHERE w.guild_id::text=$1 AND w.discord_user_id::text=$2 LIMIT 1
        """, str(guild_id), str(discord_user_id))
        if not person:
            return False
        result = await self.db.execute("DELETE FROM hll_personnel_links WHERE personnel_id=$1", person["personnel_id"])
        return bool(result and not str(result).endswith(" 0"))

    async def research_snapshot(self, guild_id: int, discord_user_id: int) -> Optional[dict]:
        person = await self.db.fetchrow("""
            SELECT p.id::text AS personnel_id,p.rank_code,p.first_name,p.last_name FROM personnel p
            JOIN website_member_links w ON w.personnel_id=p.id::text
            WHERE w.guild_id::text=$1 AND w.discord_user_id::text=$2 LIMIT 1
        """, str(guild_id), str(discord_user_id))
        if not person:
            return None
        row = await self.db.fetchrow("""
            SELECT rs.*,rm.verified,rm.verified_role_name,rm.role_category,rm.mos_code
            FROM hll_research_samples rs LEFT JOIN hll_role_mappings rm ON rm.role_id=rs.role_id
            WHERE rs.personnel_id=$1 ORDER BY rs.observed_at DESC LIMIT 1
        """, person["personnel_id"])
        return {"person":dict(person),"sample":dict(row) if row else None}

    async def role_research_summary(self) -> list[dict]:
        rows = await self.db.fetch("""
            SELECT rm.*,COUNT(DISTINCT rlo.loadout)::int AS loadout_count,
                   COALESCE(MAX(rlo.max_speed_mps),0)::double precision AS max_speed_mps,
                   COALESCE(MAX(rlo.max_vertical_speed_mps),0)::double precision AS max_vertical_speed_mps,
                   COALESCE(SUM(rlo.high_speed_seconds),0)::bigint AS high_speed_seconds,
                   COALESCE(SUM(rlo.altitude_gain_meters),0)::double precision AS altitude_gain_meters
            FROM hll_role_mappings rm LEFT JOIN hll_role_loadout_observations rlo ON rlo.role_id=rm.role_id
            GROUP BY rm.role_id ORDER BY rm.verified DESC,rm.sample_count DESC,rm.role_id
        """)
        return [dict(r) for r in rows]

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
                   COALESCE(SUM(team_kills),0)::bigint AS blue_on_blue,
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
        leadership_seconds={"9":0,"12":0,"17":0}
        role_rows=await self.db.fetch("SELECT role_seconds FROM hll_player_match_stats WHERE personnel_id=$1", person["personnel_id"])
        for rr in role_rows or []:
            rd=_json_dict(rr.get("role_seconds"))
            for rid in leadership_seconds:
                leadership_seconds[rid] += int(rd.get(rid,0) or 0)
        aggregate["leadership_seconds"]=leadership_seconds
        aggregate["leadership_total_seconds"]=sum(leadership_seconds.values())
        m16=await self.db.fetchrow("""SELECT COALESCE(SUM(m16_carried_seconds),0)::bigint AS seconds,
                    COALESCE(SUM(m16_distance_meters),0)::double precision AS distance,
                    COUNT(*) FILTER (WHERE COALESCE(m16_carried_seconds,0)>0)::int AS rounds
             FROM hll_player_match_stats WHERE personnel_id=$1""", person["personnel_id"])
        m16_events=await self.db.fetchrow("""SELECT COUNT(*) FILTER(WHERE is_m16=TRUE AND event_type='KILL')::int AS kills,
                    COUNT(*) FILTER(WHERE is_m16=TRUE AND event_type='BLUE_ON_BLUE')::int AS blue_on_blue,
                    MAX(event_at) FILTER(WHERE is_m16=TRUE) AS last_event
             FROM hll_weapon_events WHERE personnel_id=$1""", person["personnel_id"])
        aggregate["m16_service"]={**(dict(m16) if m16 else {}),**(dict(m16_events) if m16_events else {})}
        hours=float(aggregate.get("seconds") or 0)/3600.0
        matches=int(aggregate.get("matches") or 0)
        if hours >= 40 or matches >= 25: field_experience="VETERAN"
        elif hours >= 15 or matches >= 10: field_experience="COMBAT TESTED"
        elif hours >= 5 or matches >= 3: field_experience="FIELD EXPERIENCED"
        else: field_experience="NEWLY ARRIVED"
        aggregate["field_experience"]=field_experience
        aggregate["score_total"]=sum(int(aggregate.get(k) or 0) for k in ("combat_score","defense_score","offense_score","support_score"))
        return {"person": dict(person), "link": dict(link) if link else None, "aggregate": aggregate, "latest": dict(latest) if latest else None}
