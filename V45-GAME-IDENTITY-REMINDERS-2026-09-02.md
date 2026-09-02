# V45 — Game Identity Reminder Automation

- Battalion Clerk checks established Discord Members for missing HLL: Vietnam game identity links once per hour.
- Unlinked Members receive a private reminder no more than once every 3 days (72 hours by default).
- Reminder cadence is persisted in PostgreSQL table `clerk_game_link_reminders`, so Railway/bot restarts do not reset the cooldown or cause duplicate reminder bursts.
- Verified `hll_personnel_links` stop reminders automatically.
- Pending/verified console claims in `hll_identity_claims` also stop reminders because the member has completed `/link-game` and is waiting only for server observation.
- Replacement applicants are excluded; the recurring reminder applies to members with the Discord `Member` role.
- Successful `/link-game` explicitly clears any prior reminder state.
- Reminder text tells members they may use `/link-game` in any text channel in the 1/5 Cavalry Discord and explains that server time, seeding, ribbons, campaign progress, and supported telemetry require a link.
- Optional environment variable: `GAME_LINK_REMINDER_DAYS`; default `3`, minimum `1`.
