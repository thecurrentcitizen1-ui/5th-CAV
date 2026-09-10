V91 — BATTALION CLERK RELIABILITY + RECOVERY V2 — 2026-09-09

- Added /system-health for a green/amber/red Command summary across Website/Database, Battalion Clerk, Discord sync, Recruiting, Awards, HLL/RCON telemetry and game-identity integrity.
- Added /repair-system to trigger a safe Website/Clerk reconciliation and report exactly what was requeued/repaired.
- Added a 10-minute automatic recovery watch for WARN/FAIL health states.
- Retryable failures are retried; repeated failures are quarantined temporarily to prevent Command alert spam.
- Failed Discord role-sync jobs below the terminal-attempt threshold are safely requeued.
- Approved recruit/login delivery failures are re-opened for the existing Battalion Clerk delivery pipeline.
- Existing stuck-accession safeguards are invoked automatically through the Website recovery endpoint.
- Duplicate/conflicting game identities remain detection-only; Clerk does not guess which Soldier owns an identity.
- Stale RCON/server telemetry remains detection/alert only because credentials/connectivity require operator correction.
- Weekly Battalion Brief posting now retries failed sends instead of permanently consuming the weekly delivery key on the first failure.
- Weekly Brief retries are capped and quarantined after repeated failure to avoid spam.
