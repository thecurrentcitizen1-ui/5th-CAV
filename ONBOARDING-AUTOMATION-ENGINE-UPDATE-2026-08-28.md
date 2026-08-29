# Unified Onboarding Automation Engine — 28 AUG 2026

- Added `/link-game` as the single member-facing HLL: Vietnam identity command for Steam, Xbox, and PlayStation.
- Steam identities verify immediately when valid. Console identities can file a safe pending claim and are verified automatically when the exact account is observed by Battalion Clerk on the unit server.
- Duplicate/conflicting identities remain blocked instead of silently moving service between Soldier records.
- Linking a Steam identity backfills telemetry collected before the link so eligible existing server history can attach to the Soldier.
- Welcome Packet delivery now sends assignment and initial-onboarding-complete events to the appropriate Soldier Record destination rather than always pointing at the packet.
- Legacy self-link aliases were later retired on 2026-08-29. Use `/link-game`; staff use `/hll-link-soldier`.
