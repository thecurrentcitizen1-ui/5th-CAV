# Battalion Clerk V41 — Game Identity Unlink

- Added member-facing `/unlink-game` as the companion command to `/link-game`.
- `/unlink-game` removes the Soldier's current `hll_personnel_links` identity mapping and supersedes any pending console identity claim.
- Historical HLL telemetry, match rows, research samples, Soldier Record data, awards, assignments, and service history are intentionally preserved.
- Members can immediately run `/link-game` again with the correct SteamID64, Xbox Gamertag, or PSN Online ID.
- Existing `/hll-unlink` remains available as a legacy alias and now uses the same safe identity-only behavior.
- If a previously incorrect identity already caused another player's telemetry to be credited to the Soldier, that data requires a separate staff repair; unlinking does not silently delete or reassign history.
