# V61 — Training Scheduler / RSVP / HLL Attendance

- Adds `/schedule-training` for authorized 1/5 Cav leaders.
- Posts a persistent Discord RSVP roster with ATTENDING / MAYBE / NOT ABLE buttons.
- Roster message edits live as members change RSVP status.
- Uses current Soldier rank/name from the website where linked.
- Adds `/training-events`, `/training-roster`, `/training-credit`, and `/close-training`.
- Adds 60-minute and 15-minute reminders plus training-window start/end notices.
- Battalion Clerk triggers website reconciliation every minute; the website derives actual training minutes from verified 1/5 Cav HLL telemetry.
- Persistent RSVP buttons survive bot restarts by resolving the event from the Discord message ID.
