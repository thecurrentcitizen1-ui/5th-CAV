# Training Scheduler Quick Test

1. Deploy Website V148 and Battalion Clerk V61 together.
2. Use `/schedule-training` with a start time 15-20 minutes in the future and a 15-minute duration.
3. From a linked test member, press ATTENDING. Confirm the name appears under ATTENDING.
4. Change to MAYBE, then back to ATTENDING. Confirm the roster moves the member rather than duplicating them.
5. Run `/training-events` and copy the event ID.
6. Run `/training-roster event_id:<id>` and confirm RSVP state.
7. During the scheduled window, have the linked test member join the 1/5 HLL server for several minutes.
8. Run `/training-roster` again. Verified minutes should increase from HLL telemetry only.
9. Use `/training-credit` to test a host correction if desired.
10. After the window ends, run `/close-training`. The summary should show RSVP count, verified attendees, and total verified minutes.
11. Open the member's website Training Office. The event should appear in Training Attendance Ledger with credited minutes.
