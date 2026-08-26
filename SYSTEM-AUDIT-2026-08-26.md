# Battalion Clerk — System Audit 2026-08-26

This build is paired with the current Website audit build.

Verified static/integration invariants:
- Approved-recruit watcher runs every 10 seconds.
- Discord guild join immediately resumes an already-approved Recruiting Case.
- Credential DM is idempotent through `credentials_sent_at`; failed DMs remain retryable.
- Applicant intake, recruiting status, Welcome Packet notifications, personnel orders, canonical role sync, HLL/M16 reconciliation, readiness, inactivity, promotion eligibility, operation/duty automation, heartbeat, and dormant-role cleanup loops are all started on bot ready.
- Website canonical personnel state is mirrored into Discord; Discord role edits do not write assignments back into existing 201 Files.
- Team assignment remains Website-only; Company/Platoon/Squad and membership roles are canonical mirrors.
- Python compilation passes.

External deployment prerequisites remain: DISCORD_TOKEN, GUILD_ID, WEBSITE_BASE_URL, matching CLERK_SYNC_KEY, shared DATABASE_URL as intended, Server Members Intent enabled, Manage Roles permission with the Clerk role above managed roles, and HLL RCON variables when telemetry is enabled.

## Credential Delivery Recovery Control
- Added a dedicated 10-second credential_resend_watch independent of the normal recruit accession watcher.
- The recovery watcher works for Replacement and already-enlisted Soldiers and does not alter personnel assignment or recruit roles.
- Resend uses the current pending plaintext credential when available; Field Code rotation occurs only after explicit Command authorization on the Website.
- Successful/failed delivery is reported to the Website, where a permanent credential-delivery event is recorded.
