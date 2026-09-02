# V46 — Audit Hardening — 2026-09-02

Built from Battalion Clerk V45.

- Adds a deployable `requirements.txt`, including HLL: Vietnam-compatible `hllrcon` 2.0.0.x.
- Game-link reminder suppression now requires a verified `hll_personnel_links` row. Pending/verified console claims still suppress reminders because `/link-game` has already been completed.
- Documents `GAME_LINK_REMINDER_DAYS=3` in `.env.example`.
- The reminder worker still checks hourly and sends no more often than the configured 3-day cadence.
