# V65 — Training Scheduler Low-Noise + Responsive RSVP

## Changes
- Training scheduler now posts only one attendee-facing reminder: 15 minutes before start.
- The 15-minute reminder tags only RSVP members marked ATTENDING.
- 60-minute notices are ignored by the bot and never posted to the channel.
- START and END automation notices update the existing training post in place; they do not create extra channel messages.
- RSVP interactions defer immediately so Discord acknowledges button clicks before API lookups complete.
- Added a message-to-event cache to avoid a full guild event lookup on routine RSVP clicks; API lookup remains the restart-safe fallback.
- Training event embed was redesigned into a compact, mobile-friendly order with inline RSVP groups and less telemetry clutter.
- Public RSVP cards no longer append verified-minute text beside every name; verified attendance remains available to host roster/close workflows.
- Existing verified HLL attendance and training-credit logic is unchanged.

## Reminder behavior
One reminder only:
- T-15 minutes: tags ATTENDING members and tells them to report to the 1/5 server/comms.

No channel message at:
- T-60
- start time
- end time
