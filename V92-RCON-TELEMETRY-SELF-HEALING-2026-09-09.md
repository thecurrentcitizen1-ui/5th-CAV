V92 — RCON Telemetry Self-Healing — 2026-09-09

Static inspection found three reliability risks in the live HLL server collector:
1. Server #1 could silently default to disabled after a Railway redeploy when HLL_RCON_ENABLED was absent even though valid credentials existed.
2. hll_rcon_health writes used UPDATE-only behavior, so a missing health row could leave the website permanently stale until a separate schema bootstrap repaired it.
3. hllrcon response-shape changes could make get_players/get_server_session appear empty even when the server was healthy.

Fixes:
- RCON now auto-enables when host/password/port are present unless HLL_RCON_ENABLED[_N] is explicitly false.
- Health writes are now UPSERTs, recreating each server health row automatically if missing.
- Added defensive unwrapping for direct and wrapped hllrcon server-session/player-list response shapes.
- Existing retry/reconnect behavior remains intact.
