# V74 — Combat Roster Automation Toggle — 2026-09-05

Adds a Command-controlled on/off switch for the V73 Combat Voice Routing system.

## New command
- `/combat-toggle state:ON` — enables automatic match-start roster generation, combat voice movement, and match-end return behavior.
- `/combat-toggle state:OFF` — disables automatic match-start/match-end combat routing without deleting Ready Room, roster channel, side, or the nine combat voice-channel bindings.

## Behavior while OFF
- No automatic roster generation on HLL round start.
- No automatic movement into Infantry/Tank/Helicopter voice channels.
- No automatic match-end return movement.
- Existing setup and channel bindings remain saved.
- `/combat-return` remains available as an explicit emergency/manual return action.
- `/combat-status` continues to show the current Enabled YES/NO state.

The legacy `/match-formation-disable` command remains available and now points users back to `/combat-toggle state:ON` for re-enabling.
