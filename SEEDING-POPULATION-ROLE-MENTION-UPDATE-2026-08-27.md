# Battalion Clerk — Seeding Population + Regiment Mention Update

- Automatic Discord seeding calls remain suppressed whenever the live HLL population is at or above the configured ready/populated threshold.
- The bot and HLL telemetry now share the same `HLL_SEED_READY_PLAYERS` threshold (default: 40) instead of the Discord worker hard-coding a separate value.
- A legitimate low-population seeding call now mentions the exact Discord role `5th Cavalry Regiment`.
- The bot does not use `@everyone` or user mentions for these calls.
- If the regiment role is unexpectedly missing, the call still posts without a ping and records a warning rather than crashing the worker.
- Existing seeding schedules, duplicate-slot protection, RCON-current requirement, telemetry credit, Discord routing reset, VIP management, personnel sync, and all other Battalion Clerk systems remain intact.
