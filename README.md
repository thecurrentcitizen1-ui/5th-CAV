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

Current HLL / game-link commands:
- `/link-game` — primary Soldier self-link command for Steam, Xbox, or PlayStation
- `/hll-unlink` — removes the member's current game identity link
- `/hll-link-soldier` — Command/staff manual link or repair for Steam, Xbox, or PlayStation
- `/hll-rcon-status` — Command HLL collector health check
- `/hll-stats` — member field-service totals

Removed legacy commands (2026-08-29): `/hll-link`, `/hll-link-member`, `/hll-link-console`, `/hll-link-console-member`, `/schedule`, and `/operation-rounds-default`. Discord command sync removes these from the live slash-command list after deployment.

The first release is telemetry-only. It intentionally does not issue server-management RCON commands.


## 2026-08-26 Command Operating System
- Career notice delivery worker sends Website-authoritative AWARD and PROMOTION DMs exactly once.
- Operation lifecycle review worker surfaces completed operations awaiting AAR filing.
- Existing personnel role sync, progression, recruiting, telemetry and seeding workers remain unchanged.

### Command dashboards supported by this build
The Website now exposes an Accession Pipeline, Promotion Board, Personnel Sync repair controls, and System Health repair center. Battalion Clerk consumes the existing authoritative queues and additionally delivers award/promotion DMs and closed-operation AAR reminders.

## Discord channel-routing maintenance

Use `/discord-routing-reset confirmation:RESET DISCORD ROUTING` before reorganizing Discord channels. The reset pauses automatic Website/Battalion Clerk channel delivery and clears route bindings without touching personnel or existing Discord structure. Reassign channels using the normal routing commands, verify with `/discord-routing-status`, then use `/discord-routing-resume confirm:RESUME DISCORD ROUTING`.

## 2026-08-29 — Website Status Check Automation
- Battalion Clerk automatically DMs linked active Soldiers whose website login is stale.
- Default thresholds: never logged in after 3 days; last login older than 14 days; repeat check no sooner than 30 days after a response.
- Status DM uses ✅ STILL ACTIVE / ❌ NO LONGER CONTINUING reactions.
- Responses are recorded in `clerk_website_status_checks` and a staff-only service-history entry.
- CPT-and-above Discord rank holders receive a direct Command status response report.
- ❌ never automatically separates a Soldier; Command review is required.
- Reserve personnel are excluded from the active-roll website status check.
