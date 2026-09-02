# V50 — Exact Seeding Tracking — 2026-09-02

- Seeding credit window: 7:00 PM–9:00 PM America/New_York.
- Credit requires live server population below 50 players.
- 50+ players pauses credit; a later drop below 50 before 9:00 PM resumes credit.
- Discord seeding-call suppression uses the same 50-player threshold.
- New environment variable: HLL_SEED_STOP_PLAYERS=50. The older HLL_SEED_READY_PLAYERS setting is no longer used for seeding credit.
- Linked-Soldier research telemetry now stores server_player_count with each sample for exact future reconciliation.
- hll_seeding_service live upserts are capped at 7,200 seconds per Soldier/day.
