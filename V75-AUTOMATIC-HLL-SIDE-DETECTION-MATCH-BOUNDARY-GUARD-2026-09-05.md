# V75 — Automatic HLL Side Detection + Match-Boundary Guard

- Combat roster side is now detected automatically from live HLL: Vietnam telemetry.
- Battalion Clerk matches Discord members in the Ready Room to verified game identities and current live match player rows.
- The fixed combat voice net follows the majority 1/5 Cav side for that round; opposite-side players remain in the Ready Room rather than mixing opposing teams in one Discord squad.
- Helicopter 1–3 remain hard U.S.-side-only. NVA combat rosters never create helicopter elements.
- Unlinked/unmatched Ready Room members are not moved automatically and are listed in a routing notice.
- Added a persistent activation baseline. If V75 is deployed/enabled while a match is already active, the current match is marked deferred and nobody is moved mid-game. The automation waits for that round to end and only becomes eligible on the next match start.
- At a genuine new-match boundary, Battalion Clerk waits 12 seconds for RCON team telemetry to settle, then generates/moves the roster once.
- `/combat-side` now supports `Automatic — detect from HLL server` plus U.S./NVA manual fallback modes.
- `/combat-status` now shows automatic side detection and mid-game protection state.
- Turning `/combat-toggle ON` resets the activation baseline so enabling during a live game safely defers that game.
