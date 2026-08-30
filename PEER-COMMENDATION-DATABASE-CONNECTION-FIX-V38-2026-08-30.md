# Peer Commendation Database Connection Fix — V38

- Fixed `/commend` referencing an undefined module-level `db` variable.
- Commendation initialization now starts/reuses `collector` and uses `collector.db`, the Battalion Clerk's real shared PostgreSQL connection.
- Canonical recipient/giver resolution and the commendation insert use the same connected database handle.
- Startup commendation table initialization now occurs through the live collector connection.
- Preserves V37 timeout/error handling, V36 stable Discord identity fields, and all prior cumulative bot features.
