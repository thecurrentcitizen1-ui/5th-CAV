# V58 — Manual Intake Global Queue Delivery

- Rebuilt manual Replacement Interview delivery around a single global website queue.
- Removed per-guild queue filtering that could strand Command-adopted cases with blank/stale guild_id values.
- Resolves recruits by preferred guild and falls back to any configured battalion guild where the Discord user is present.
- Added explicit queue, member-resolution, DM-blocked, success, and exception logging.
- Added a ready-gate before the 5-second delivery watcher begins.
