"""Compatibility layer for HLL: Vietnam Microsoft-PC identity linking.

This module patches Battalion Clerk's HLLVTelemetryCollector at process startup.
It keeps the existing durable RCON player-key model, but removes the incorrect
assumption that a Microsoft/Xbox ecosystem player must have an RCON Platform
label containing the literal word "xbox".
"""
from __future__ import annotations

import logging
from typing import Any

import hllv_rcon

log = logging.getLogger("battalion-clerk.identity-compat")

_PATCH_VERSION = "V304-MICROSOFT-PC-IDENTITY"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _platform_family(value: Any) -> str:
    """Classify an observed RCON platform label without over-trusting it."""
    raw = _text(value).casefold()
    compact = "".join(ch for ch in raw if ch.isalnum())
    if not compact:
        return "UNKNOWN"

    # Definitive Steam labels must never be silently accepted for an Xbox claim.
    if "steam" in compact:
        return "STEAM"

    # HLLV / GDK builds have used several Microsoft-PC labels. These are all the
    # same account ecosystem for linking purposes even when the player is on PC.
    xbox_tokens = (
        "xbox", "microsoft", "msstore", "microsoftstore", "windowsstore",
        "gamepass", "xboxapp", "gdk", "wingdk", "xboxpc", "pcxbox",
    )
    if any(token in compact for token in xbox_tokens):
        return "XBOX"

    if any(token in compact for token in ("playstation", "ps5", "psn", "sony")):
        return "PS5"

    # Generic PC/Windows/EOS labels do not prove Steam vs Microsoft Store. They
    # are eligible only for the exact-name + single-durable-ID fallback below.
    if compact in {
        "pc", "windows", "win", "win32", "win64", "eos", "epic", "unknown",
        "desktop", "computer",
    }:
        return "NEUTRAL"

    return "UNKNOWN"


def _canonical_player_key(row: Any) -> str:
    """Use the same durable key already used by the telemetry tables."""
    return _text(row.get("steam_id") if row else "")


async def _exact_identity_candidates(self, visible_name: str) -> list[dict]:
    rows = await self.db.fetch(
        """
        SELECT steam_id,player_name,platform,platform_user_id,eos_id,last_seen_at
        FROM hll_player_match_stats
        WHERE LOWER(TRIM(COALESCE(player_name,'')))=LOWER(TRIM($1))
        ORDER BY last_seen_at DESC NULLS LAST
        LIMIT 100
        """,
        visible_name,
    )

    # A player appears once per match. Collapse historical rows to one latest row
    # per durable key so repeated matches do not look like duplicate identities.
    by_key: dict[str, dict] = {}
    for row_obj in rows:
        row = dict(row_obj)
        key = _canonical_player_key(row)
        if not key:
            continue
        if key not in by_key:
            by_key[key] = row
    return list(by_key.values())


async def _resolve_visible_identity(self, requested_platform: str, visible_name: str):
    requested = _text(requested_platform).upper()
    candidates = await _exact_identity_candidates(self, visible_name)
    if not candidates:
        return None, "NOT_OBSERVED", (
            f"No {requested} player named '{visible_name}' has been observed by Battalion Clerk yet. "
            "Have them join the 1/5 CAV server once, then run this command again."
        )

    matching = [r for r in candidates if _platform_family(r.get("platform")) == requested]
    if len(matching) == 1:
        return matching[0], "PLATFORM_FAMILY", None
    if len(matching) > 1:
        return None, "AMBIGUOUS", (
            f"Multiple durable HLL identities named '{visible_name}' were observed for {requested}. "
            "Battalion Clerk will not guess. Command/S-1 must review the identity records."
        )

    # Exact-name fallback: accept neutral/unknown platform labels only when there
    # is exactly one durable observed identity and no definitive conflicting
    # platform. This is the Microsoft-PC/GDK hole the old matcher could not pass.
    non_conflicting = []
    conflicting = []
    for row in candidates:
        family = _platform_family(row.get("platform"))
        if family in {"NEUTRAL", "UNKNOWN", requested}:
            non_conflicting.append(row)
        else:
            conflicting.append(row)

    if len(non_conflicting) == 1:
        return non_conflicting[0], "UNIQUE_EXACT_NAME", None
    if len(non_conflicting) > 1:
        return None, "AMBIGUOUS", (
            f"More than one durable HLL identity uses the exact in-game name '{visible_name}'. "
            "Battalion Clerk will not attach one automatically. Command/S-1 must review it."
        )

    observed = ", ".join(sorted({_text(r.get("platform")) or "UNKNOWN" for r in conflicting})) or "UNKNOWN"
    return None, "PLATFORM_CONFLICT", (
        f"The exact in-game name '{visible_name}' was observed, but only on a conflicting platform label ({observed}). "
        "Choose the correct platform or contact Command/S-1; Battalion Clerk will not cross-link a definitive platform mismatch."
    )


async def _patched_link_console_personnel(self, guild_id: int, discord_user_id: int, platform: str, gamertag: str, linked_by: str) -> dict:
    await self.collector.start()
    platform = _text(platform).upper()
    gamertag = _text(gamertag)
    if platform not in {"XBOX", "PS5"}:
        return {"ok": False, "error": "Platform must be Xbox/Microsoft Store PC or PlayStation 5."}
    if not gamertag:
        return {"ok": False, "error": "Enter the exact in-game Xbox/Microsoft gamertag or PSN Online ID."}

    person = await self._person_for_discord(guild_id, discord_user_id)
    if not person:
        return {"ok": False, "error": "No active Soldier Record is linked to that Discord account."}

    row, matched_by, error = await _resolve_visible_identity(self, platform, gamertag)
    if not row:
        return {"ok": False, "error": error, "match_state": matched_by}

    player_key = _canonical_player_key(row)
    if not player_key:
        return {"ok": False, "error": "The server observed that exact player name but did not expose a durable player ID. Try again while the player is in the server."}

    personnel_id = _text(person["personnel_id"])
    existing_identity = await self.db.fetchrow(
        "SELECT personnel_id FROM hll_personnel_links WHERE steam_id=$1 LIMIT 1", player_key
    )
    if existing_identity and _text(existing_identity.get("personnel_id")) != personnel_id:
        return {"ok": False, "error": "That HLL identity is already linked to another Soldier Record. Command/S-1 must resolve the ownership conflict."}

    owned_identity = await self.db.fetchrow(
        "SELECT steam_id FROM hll_personnel_links WHERE personnel_id=$1 LIMIT 1", personnel_id
    )
    if owned_identity and _text(owned_identity.get("steam_id")) != player_key:
        return {"ok": False, "error": "Your Soldier Record still has a different game identity on file. Run /unlink-game first, then retry /link-game."}

    try:
        if existing_identity:
            await self.db.execute(
                """
                UPDATE hll_personnel_links
                   SET discord_user_id=$1,hll_player_name=$2,platform=$3,
                       platform_user_id=$4,eos_id=$5,linked_by=$6,verified=TRUE,updated_at=NOW()
                 WHERE steam_id=$7 AND personnel_id=$8
                """,
                str(discord_user_id), row.get("player_name"), row.get("platform"),
                row.get("platform_user_id"), row.get("eos_id"), linked_by,
                player_key, personnel_id,
            )
        else:
            await self.db.execute(
                """
                INSERT INTO hll_personnel_links(
                    steam_id,personnel_id,discord_user_id,hll_player_name,platform,
                    platform_user_id,eos_id,linked_by,verified,updated_at
                ) VALUES($1,$2,$3,$4,$5,$6,$7,$8,TRUE,NOW())
                """,
                player_key, personnel_id, str(discord_user_id), row.get("player_name"),
                row.get("platform"), row.get("platform_user_id"), row.get("eos_id"), linked_by,
            )

        # Claim only telemetry that is unowned or already belongs to this Soldier.
        await self.db.execute(
            "UPDATE hll_player_match_stats SET personnel_id=$1 WHERE steam_id=$2 AND (personnel_id IS NULL OR personnel_id=$1)",
            personnel_id, player_key,
        )
        try:
            await self.db.execute(
                "UPDATE hll_research_samples SET personnel_id=$1 WHERE steam_id=$2 AND (personnel_id IS NULL OR personnel_id=$1)",
                personnel_id, player_key,
            )
        except Exception:
            pass
    except Exception as exc:
        return {"ok": False, "error": f"Link conflict: {exc}"}

    name = f"{person.get('rank_code') or ''} {person.get('first_name') or ''} {person.get('last_name') or ''}".strip()
    log.info(
        "[HLL IDENTITY LINK %s] personnel=%s name=%s requested=%s observed_platform=%s player_key_present=%s",
        matched_by, personnel_id, row.get("player_name"), platform, row.get("platform"), bool(player_key),
    )
    return {
        "ok": True,
        "status": "VERIFIED",
        "verified": True,
        "personnel_id": personnel_id,
        "soldier": name,
        "player_name": row.get("player_name"),
        "platform": platform,
        "observed_platform": row.get("platform"),
        "player_key": player_key,
        "matched_by": matched_by,
    }


async def _finish_pending_console_claim(self, claim: Any) -> bool:
    platform = _text(claim.get("platform")).upper()
    identity = _text(claim.get("claimed_identity"))
    personnel_id = _text(claim.get("personnel_id"))
    discord_user_id = _text(claim.get("discord_user_id")) or None
    if platform not in {"XBOX", "PS5"} or not identity or not personnel_id:
        return False

    row, matched_by, error = await _resolve_visible_identity(self, platform, identity)
    if not row:
        if matched_by == "NOT_OBSERVED":
            return False
        # Ambiguity or a definitive platform conflict requires human review;
        # do not keep retrying a claim that can no longer be resolved safely.
        await self.db.execute(
            "UPDATE hll_identity_claims SET status='CONFLICT',error=$1,updated_at=NOW() WHERE id=$2",
            error[:500], claim["id"],
        )
        return False

    player_key = _canonical_player_key(row)
    if not player_key:
        return False

    conflict = await self.db.fetchrow(
        "SELECT personnel_id FROM hll_personnel_links WHERE steam_id=$1", player_key
    )
    if conflict and _text(conflict.get("personnel_id")) != personnel_id:
        await self.db.execute(
            "UPDATE hll_identity_claims SET status='CONFLICT',error=$1,updated_at=NOW() WHERE id=$2",
            "Observed HLL account is already linked to another Soldier", claim["id"],
        )
        return False

    owned = await self.db.fetchrow(
        "SELECT steam_id FROM hll_personnel_links WHERE personnel_id=$1 LIMIT 1", personnel_id
    )
    if owned and _text(owned.get("steam_id")) != player_key:
        await self.db.execute(
            "UPDATE hll_identity_claims SET status='CONFLICT',error=$1,updated_at=NOW() WHERE id=$2",
            "Soldier already linked to a different HLL identity", claim["id"],
        )
        return False

    await self.db.execute(
        """
        INSERT INTO hll_personnel_links(
            steam_id,personnel_id,discord_user_id,hll_player_name,platform,
            platform_user_id,eos_id,linked_by,verified,updated_at
        ) VALUES($1,$2,$3,$4,$5,$6,$7,'IDENTITY COMPAT AUTO-VERIFY',TRUE,NOW())
        ON CONFLICT(steam_id) DO UPDATE SET
            personnel_id=EXCLUDED.personnel_id,
            discord_user_id=EXCLUDED.discord_user_id,
            hll_player_name=EXCLUDED.hll_player_name,
            platform=EXCLUDED.platform,
            platform_user_id=EXCLUDED.platform_user_id,
            eos_id=EXCLUDED.eos_id,
            linked_by='IDENTITY COMPAT AUTO-VERIFY',verified=TRUE,updated_at=NOW()
        """,
        player_key, personnel_id, discord_user_id, row.get("player_name"), row.get("platform"),
        row.get("platform_user_id"), row.get("eos_id"),
    )
    await self.db.execute(
        "UPDATE hll_player_match_stats SET personnel_id=$1 WHERE steam_id=$2 AND (personnel_id IS NULL OR personnel_id=$1)",
        personnel_id, player_key,
    )
    try:
        await self.db.execute(
            "UPDATE hll_research_samples SET personnel_id=$1 WHERE steam_id=$2 AND (personnel_id IS NULL OR personnel_id=$1)",
            personnel_id, player_key,
        )
    except Exception:
        pass
    await self.db.execute(
        "UPDATE hll_identity_claims SET status='VERIFIED',linked_player_key=$1,error=NULL,linked_at=NOW(),updated_at=NOW() WHERE id=$2",
        player_key, claim["id"],
    )
    try:
        if claim.get("recruiting_case_id"):
            await self.db.execute(
                "UPDATE recruiting_cases SET game_identity_link_status='VERIFIED',game_identity_link_error=NULL,game_identity_linked_at=NOW(),updated_at=NOW() WHERE id=$1",
                claim.get("recruiting_case_id"),
            )
    except Exception:
        pass

    log.info(
        "[HLL PENDING CLAIM %s] claim=%s personnel=%s name=%s requested=%s observed_platform=%s",
        matched_by, claim.get("id"), personnel_id, row.get("player_name"), platform, row.get("platform"),
    )
    return True


def install() -> None:
    cls = hllv_rcon.HLLVTelemetryCollector
    if getattr(cls, "_identity_compat_patch_version", None) == _PATCH_VERSION:
        return

    original_reconcile = cls._reconcile_pending_identity_claims

    async def patched_reconcile(self):
        # Preserve all existing Steam and recognized-console behavior first.
        await original_reconcile(self)
        claims = await self.db.fetch(
            """
            SELECT * FROM hll_identity_claims
            WHERE status='PENDING' AND UPPER(COALESCE(platform,'')) IN ('XBOX','PS5')
            ORDER BY created_at ASC
            LIMIT 100
            """
        )
        for claim in claims:
            try:
                await _finish_pending_console_claim(self, claim)
            except Exception as exc:
                log.warning(
                    "[HLL IDENTITY COMPAT PENDING] claim=%s platform=%s error=%s",
                    claim.get("id"), claim.get("platform"), exc,
                )
                try:
                    await self.db.execute(
                        "UPDATE hll_identity_claims SET error=$1,updated_at=NOW() WHERE id=$2",
                        str(exc)[:500], claim["id"],
                    )
                except Exception:
                    pass

    cls.link_console_personnel = _patched_link_console_personnel
    cls._reconcile_pending_identity_claims = patched_reconcile
    cls._identity_compat_patch_version = _PATCH_VERSION
    log.info("[HLL IDENTITY COMPAT] installed %s", _PATCH_VERSION)


install()
