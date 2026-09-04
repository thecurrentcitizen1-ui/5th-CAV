# V59 — Recruit Intake Verification + Test Tools

- Manual Replacement Interview worker continues to consume one global website queue before Discord guild resolution.
- Shared Replacement Interview message helper is now used by both real queue delivery and direct testing.
- Added `/recruit-intake-health` (Manage Server required): confirms Website queue connectivity, pending count, sample cases, and battalion guild configuration.
- Added `/test-recruit-intake member:@user` (Manage Server required): sends the exact Replacement Interview DM directly, bypassing the Website queue, so Discord DM delivery can be tested independently.
- Persistent BEGIN / RESUME INTAKE button remains registered across bot restarts.
- Mock integration test passed for null-guild delivery, stale-guild recovery, blocked-DM reporting, and adopted-case resume behavior.
