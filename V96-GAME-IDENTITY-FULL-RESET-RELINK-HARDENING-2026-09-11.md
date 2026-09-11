# V96 — Game Identity Full Reset / Re-Link Hardening
Date: 2026-09-11

## Repairs
- `/unlink-game`, `/hll-unlink`, and staff unlink now retire **all** active identity claims (`PENDING`, `VERIFIED`, and `LINKED`), not only pending console claims.
- Unlink now marks the recruiting-case identity fallback `REVOKED`, preserving the original intake value for audit while preventing the website from resurrecting the wrong gamertag/SteamID as an active link.
- `/link-game` no longer steals a Steam/platform identity already owned by another Soldier Record.
- A Soldier with a different current identity must explicitly unlink/reset before a new identity can be filed.
- Telemetry backfill is limited to unclaimed rows or rows already owned by the same Soldier; link repair will not move another Soldier's credited history.
- Historical telemetry and the Soldier Record remain preserved during unlink/reset.

## Result
A member who entered the wrong gamertag/SteamID can now fully clear the incorrect identity and immediately run `/link-game` again without stale verified claims or application fallback records blocking the correction.
