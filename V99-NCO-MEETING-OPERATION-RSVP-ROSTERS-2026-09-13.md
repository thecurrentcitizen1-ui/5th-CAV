# V99 — NCO Meeting + Operation RSVP Rosters

Date: 2026-09-13

## Purpose
Add persistent Discord RSVP rosters to the two V97 scheduling workflows that did not yet have attendee responses:
- `/schedule-nco-meeting`
- `/schedule-operation`

## Behavior
### NCO meetings
The initial NCO meeting notice now includes three persistent buttons:
- ATTENDING
- MAYBE
- NO

The post updates in place with live names and counts. The NCO Discord role is still the only role pinged. RSVP actions are restricted to the NCO Corps and authorized leadership.

### Operations / campaigns / special events
The scheduled Operation notice now includes the same ATTENDING / MAYBE / NO roster. The Operation post displays the live roster and keeps the existing operation schedule, voice-channel, attendance, telemetry, M16, and website Operation-record behavior intact.

## Persistence
Two small Battalion Clerk PostgreSQL tables retain:
- scheduled Discord message -> authoritative event linkage
- one current RSVP per member per event

Members can change their response until the scheduled start time. Responses survive bot restarts because the RSVP view is registered as a persistent Discord view and the event/message linkage is stored in PostgreSQL.

## Scope
This is a Battalion Clerk-only change. Website V302 remains compatible and does not need another website package for this RSVP feature.
