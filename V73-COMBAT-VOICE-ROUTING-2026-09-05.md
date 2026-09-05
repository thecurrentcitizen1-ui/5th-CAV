# V73 — Combat Voice Routing

## Purpose
Extends Battalion Clerk's randomized combat roster into automatic Discord voice routing while keeping permanent website formations untouched.

## Fixed combat nets
- Infantry 1 / Infantry 2 / Infantry 3
- Tank 1 / Tank 2 / Tank 3
- Helicopter 1 / Helicopter 2 / Helicopter 3

## Match flow
1. Members muster in the configured Ready Room.
2. On detection of a new live HLL match, 6+ mustered members are randomized into the available combat elements.
3. Battalion Clerk posts the Combat Roster and moves each assigned member to the voice channel bound to that element.
4. When the match ends, Battalion Clerk returns members from all configured combat nets to the Ready Room.
5. The next live match creates a new random roster.

## U.S. / NVA rule
Helicopter elements are hard-disabled whenever `/combat-side` is set to NVA. U.S. side can activate Helicopter 1–3 at attendance thresholds.

## Specialty thresholds
- Tank 1: 9+
- Tank 2: 18+
- Tank 3: 27+
- Helicopter 1: 13+ (U.S. only)
- Helicopter 2: 25+ (U.S. only)
- Helicopter 3: 37+ (U.S. only)

Infantry is capped at three six-player combat squads. Personnel beyond the nine configured combat-net capacity remain in the Ready Room as standby rather than overfilling an HLL squad.

## Commands
- `/combat-setup ready_room roster_channel`
- `/combat-channel element channel`
- `/combat-side side`
- `/combat-status`
- `/combat-generate`
- `/combat-return`

Legacy `/match-formation-*` commands remain available for compatibility.

## Permissions
The bot needs View Channel and Move Members permissions for the Ready Room and all configured combat voice channels.
