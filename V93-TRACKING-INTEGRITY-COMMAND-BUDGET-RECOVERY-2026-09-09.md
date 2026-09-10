V93 — Tracking Integrity + Command Budget Recovery — 2026-09-09

Critical production fix
- Railway log showed Battalion Clerk crashing during import because Discord rejected the 101st global slash command.
- Consolidated diagnostics into top-level groups so the bot now has 97 top-level slash commands, below Discord's 100-command limit.
  - /system health | tracking | repair | channel
  - /seeding set | status | clear
  - /hll status | research | role-research | stats
- Added a startup command-budget guard and log line so a future build cannot silently exceed the Discord limit again.

Tracking / telemetry hardening
- One malformed HLL player payload no longer aborts the entire poll. Good Soldier samples continue filing.
- One malformed weapon/admin-log event no longer stops weapon/stat telemetry.
- RCON health now records players seen, players successfully filed, partial player errors, and the first partial error.
- Same-map/rematch timer resets are detected so two rounds on the same layer are not merged into one match ledger.
- M16 carried time/distance now accrues only when an M16/XM16 is actually detected in loadout/weapon data.
- Existing reconnect, player-payload wrapper handling, dual-server telemetry, identity linking, seeding, commander-ledger, and member-stat paths remain intact.

Safe recovery
- /system tracking audits member telemetry numbers, identity attribution, open match ledgers, seeding credit, and partial RCON sample errors.
- /system repair invokes the Website's mechanically safe repair pass; it never guesses identity ownership, match winners, awards, rank, or assignment decisions.
