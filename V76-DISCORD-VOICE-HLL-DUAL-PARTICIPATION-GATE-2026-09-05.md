# V76 — Discord Voice + HLL Dual Participation Gate

## Purpose
Combat Roster participation now uses both Discord voice presence and live HLL telemetry.

## Rules
- A member must be physically present in the configured `1/5 CAV — READY ROOM` to opt into the next Combat Roster.
- The member must also be matched through a verified game identity and detected in the live HLL round before Battalion Clerk automatically routes them.
- Live HLL telemetry determines U.S. vs NVA side. Discord presence never guesses faction.
- Members in HLL who are not in the Ready Room are not touched.
- Members in the Ready Room who are not game-linked remain in the Ready Room and are reported as `game identity not linked`.
- Members who are linked but are not detected in the live HLL round remain in the Ready Room and are reported separately.
- A live player whose faction is not resolved remains in the Ready Room.

## Mid-match guard
- Joining the Ready Room during an active round does not trigger movement or a reshuffle.
- Those members are recorded as queued for the next match.
- The queue is cleared at the match boundary and all eligibility is re-validated fresh for the next round.
- Existing combat squads are never rebuilt because of a mid-round voice join/leave.

## Status
`/combat-status` now reports:
- Participation gate: Discord Ready Room + Live HLL
- Live Ready Room muster
- Number of members queued during the current round
- Automatic side detection and mid-game protection state

## Safety
The bot never infers a game identity or faction from a Discord display name.
