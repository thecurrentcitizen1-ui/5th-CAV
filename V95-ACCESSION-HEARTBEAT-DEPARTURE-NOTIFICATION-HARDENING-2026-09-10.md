# V95 — Accession, Heartbeat & Departure Notification Hardening

## Telemetry heartbeat
- Added an independent per-server RCON heartbeat task.
- Added heartbeat_at, last_poll_started_at and last_poll_finished_at health fields.
- Heartbeat continues to update while an RCON command is stalled, separating worker liveness from server/RCON success.
- Added configurable RCON command timeout (default 20s) around connect, session, players, admin log and broadcast operations.
- Connect timeouts now fail the poll explicitly instead of being swallowed as an auto-connect compatibility exception.

## Discord intake / 201 self-healing
- Added 30-second accession integrity watch.
- Automatically asks the Website to relink/recover completed orphaned Discord intakes into Command review.
- Automatically retries approved accessions that are missing the 201 File / Battle Roster card / Discord personnel link chain.
- Normal approval authority is preserved; completed intake alone never auto-approves a recruit.

## Member departure DMs
- When a human member leaves the Discord, Battalion Clerk DMs every current holder of either Command Staff or Admin.
- Recipients holding both roles receive one message.
- DM includes departing member, Discord identity, Battle Roster number, last assignment, cleanup result, and departure time when available.
- A failed DM never blocks the personnel purge or other staff notifications.

## Command budget
- No new slash commands were added.
