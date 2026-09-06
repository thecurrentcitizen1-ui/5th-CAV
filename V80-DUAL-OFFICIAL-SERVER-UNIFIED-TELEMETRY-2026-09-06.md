# V80 — Dual Official Server Unified Telemetry

## Purpose
Adds a second official HLL: Vietnam RCON telemetry collector without splitting member-facing career statistics.

## Railway variables
Server #1 remains unchanged:
- HLL_RCON_HOST
- HLL_RCON_PORT
- HLL_RCON_PASSWORD

Server #2:
- HLL_RCON_HOST_2
- HLL_RCON_PORT_2
- HLL_RCON_PASSWORD_2

Optional:
- HLL_RCON_ENABLED_2=true (defaults enabled when credentials are present)
- HLL_RCON_SEEDING_ENABLED_2=false (defaults false)

## Behavior
- Both official servers write to the same HLL personnel/match telemetry tables.
- Member 201 File totals remain unified; no Server #1 / Server #2 breakdown is exposed.
- Match rows retain a backend-only server_key so simultaneous matches and restarts cannot merge two servers into one match ledger.
- Server #2 is telemetry-only for now. VIP sync, manual RCON broadcasts, and other administrative RCON commands remain anchored to Server #1.
- Server #2 does not award seeding credit by default, preventing duplicate/accidental seeding awards. It can be enabled separately later.
- Each RCON connection has its own health row.
