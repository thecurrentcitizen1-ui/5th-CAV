# V87 — Combat Roster Retry-Safe Pending Match Repair

## Fixed
- New Server #1 matches are no longer marked processed before a Combat Roster actually files.
- A newly observed live match becomes `pending_match_id` and is retried every 15 seconds.
- Low Ready Room population, incomplete HLL telemetry, unresolved faction data, or temporary lookup errors no longer permanently discard the round.
- `last_processed_match_id` advances only after `_publish_match_formation()` succeeds.
- Pending state clears when the live match ends or after a successful roster filing.
- Existing Server #1 authority from V86 remains intact; Server #2 cannot drive combat roster boundaries.
- Existing 6-player roster / 7+ automatic voice-move thresholds remain unchanged.
- Existing mid-round activation baseline protection remains unchanged.
- Manual `/combat-generate` remains available for an explicit current-round staff test.

## Diagnostics
- `/combat-status` now reports the pending match and current retry reason.
- `/combat-system-check` now reports the pending auto-roster and pending reason.

## Validation
- Python compileall passed for bot.py, collector.py, config.py, database.py, and hllv_rcon.py.
