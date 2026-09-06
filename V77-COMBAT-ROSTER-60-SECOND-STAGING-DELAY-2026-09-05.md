# V77 — Combat Roster 60-Second Staging Delay

Battalion Clerk now pauses for 60 seconds after the Combat Roster is posted before moving members into the configured combat voice channels.

## Flow
1. A genuine new HLL match is detected.
2. The existing 12-second RCON telemetry settle remains in place.
3. Battalion Clerk checks the Ready Room + verified live HLL identities and determines the active 1/5 side automatically.
4. The randomized Combat Roster is posted.
5. Battalion Clerk posts a notice that automatic voice routing begins in 60 seconds.
6. After 60 seconds, members are moved into Infantry/Tank/Helicopter voice channels.

## Preserved safeguards
- Automatic U.S./NVA detection remains enabled.
- Helicopter elements remain U.S.-side only.
- Ready Room + live HLL participation gate remains enabled.
- Mid-game activation protection remains enabled.
- If the active HLL match changes during the 60-second delay, automatic movement is cancelled rather than routing a stale roster.
- Permanent website formations are never changed.
