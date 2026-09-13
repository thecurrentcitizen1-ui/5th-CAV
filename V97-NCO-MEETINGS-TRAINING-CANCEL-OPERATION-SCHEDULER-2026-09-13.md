# V97 — NCO Meetings / Training Cancellation / Operation Scheduler
Date: 2026-09-13
Website companion: V300

## New commands
### /schedule-nco-meeting
Schedules an NCO-only notification event with title, date, Eastern time, duration, optional text channel, and optional agenda notes.
- Only the Discord role named `NCO` is mentioned.
- No `@everyone`, Member, battalion, or unrelated staff-role mention is sent.
- Persistent reminders are sent at roughly 60 minutes, 15 minutes, and meeting start.
- Notice state is filed through the Website so bot restarts do not duplicate reminders.

### /cancel-training
Cancels a scheduled training by the Event ID shown by `/training-events`.
- Original training host may cancel.
- S-3 / senior leadership / Command may override.
- Future training notices stop.
- Event-specific HLL/HOST training credit is removed.
- RSVP history is retained for audit.
- The original Discord training message is changed to CANCELLED and RSVP buttons are removed.

### /schedule-operation
Schedules and publishes an authoritative Operation from Discord.
- Kinds: Operation, Campaign, Special Event.
- Requires a future date/time, duration, and Discord operation voice channel.
- Creates the Website Operation record and shared Clerk event.
- Arms scheduled Operation notice, reminders, attendance, HLL telemetry, and M16 service tracking.
- Restricted to S-3, Company senior leadership, and Command.

## Command-budget housekeeping
Retired obsolete aliases:
- `/match-formation-status` -> use `/combat-status`
- `/match-formation-publish` -> use `/combat-generate`
- `/match-formation-disable` -> use `/combat-toggle`

Current top-level slash-command budget after V97: 97 / 100.
