# 5th CAV Battalion Clerk — Strict Company / Platoon / Squad Access

This build extends strict Discord assignment visibility through all three organizational levels.

## Access model
- A Company cannot see B/C Company internal areas.
- A Company • 1st Platoon cannot see A Company • 2nd/3rd/4th Platoon areas.
- A Company • 1st Platoon • 1st Squad can see only its own squad text/voice channels inside the platoon area.
- Platoon-wide channels remain visible to every Soldier assigned to that exact platoon.
- Command, S-1, S-3, and appropriate leadership retain oversight access.
- Rank, MOS, and qualification roles remain permission-neutral.

## Migration
After deployment run:
`/strict-access-rebuild confirm:True`

The command removes legacy generic platoon/squad assignment roles where possible, creates the company/platoon/squad-specific roles, and reapplies managed channel permissions.

Then run:
`/structure-status`

Optionally run:
`/permissions-repair confirm:True`

## Hell Let Loose: Vietnam RCON telemetry

This build includes a read-only HLL: Vietnam RCON collector. The RCON password must be stored in Railway Variables and must never be committed to GitHub.

Required Railway variables:
- `HLL_RCON_ENABLED=true`
- `HLL_RCON_HOST=64.31.40.206`
- `HLL_RCON_PORT=7779`
- `HLL_RCON_PASSWORD=<private RCON password>`

Recommended defaults:
- `HLL_RCON_POLL_SECONDS=5`
- `HLL_RCON_CM_PER_METER=100`
- `HLL_RCON_MAX_SPEED_MPS=130`
- `HLL_RCON_RECONNECT_SECONDS=15`

Discord commands:
- `/hll-link` — Soldier self-links a 17-digit SteamID64
- `/hll-unlink` — removes own link
- `/hll-link-member` — staff link/repair
- `/hll-link-console` — members link their own Xbox or PlayStation account by exact in-game gamertag after they have appeared on the server
- `/hll-link-console-member` — staff override to link another Soldier to an observed console account
- `/hll-rcon-status` — Command health check
- `/hll-stats` — member field-service totals

The first release is telemetry-only. It intentionally does not issue server-management RCON commands.


## 2026-08-26 Command Operating System
- Career notice delivery worker sends Website-authoritative AWARD and PROMOTION DMs exactly once.
- Operation lifecycle review worker surfaces completed operations awaiting AAR filing.
- Existing personnel role sync, progression, recruiting, telemetry and seeding workers remain unchanged.

### Command dashboards supported by this build
The Website now exposes an Accession Pipeline, Promotion Board, Personnel Sync repair controls, and System Health repair center. Battalion Clerk consumes the existing authoritative queues and additionally delivers award/promotion DMs and closed-operation AAR reminders.
