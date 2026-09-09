# V90 — Server Watch / Dual-Server Suspected-Cheater Alerts

## Purpose
Adds a human-review suspicious-player monitoring layer to Battalion Clerk for both official HLL: Vietnam servers.

## Discord routing
- Required role name: `Server Admin` (configurable with `SERVER_ADMIN_ROLE_NAME`).
- Every non-bot Discord member holding that role receives a private DM when a live player crosses the review thresholds.
- If a member blocks bot DMs, the failure is logged and does not stop delivery to the other Server Admins.

## Servers
- Monitors `server_1` and `server_2` independently using the existing `hll_match_sessions.server_key` architecture.
- Server #2 remains telemetry-only for RCON administration; Server Watch only reads its telemetry and sends Discord notifications.

## Detection model
The system requires a minimum live sample (default 10 minutes and 25 infantry kills), then evaluates combinations of:
- sustained infantry kills per minute;
- high K/D with substantial kill volume;
- high kill count with zero recorded deaths;
- verified HLL admin-log kill bursts over 2 minutes and 5 minutes.

Alert levels:
- WATCH
- ELEVATED WATCH
- PRIORITY REVIEW

No level automatically kicks, bans, punishes, or publicly accuses a player. Every alert explicitly requires human review.

## Anti-spam
One player/match alert record is retained in PostgreSQL. A repeat DM requires the configured cooldown plus at least 10 additional kills, unless the alert escalates to a higher level.

## Commands
- `/server-watch-status` — Manage Server users can verify role recognition, recipient count, both-server RCON health, and current thresholds.
- `/server-watch-test` — sends a harmless test DM to every member holding the Server Admin role.

## Railway variables
See `.env.example`. Defaults are usable immediately; only the Discord role needs to exist. Existing Server 1 / Server 2 RCON variables remain unchanged.
