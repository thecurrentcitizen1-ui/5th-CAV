# Peer Commendation Command Reliability V37

- Removed `ensure_commendations_table()` from the live `/commend` interaction path. Schema maintenance remains startup-only.
- Added bounded identity/database operations with explicit timeout handling.
- Added top-level exception handling so a deferred Discord interaction always receives a terminal response.
- Switched cooldown/day-limit checks to stable Discord identity columns.
- Commendation inserts are transaction-protected and verify both personnel and Discord recipient identity before success is reported.
