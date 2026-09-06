# V78 — Seven-Player Automatic Voice-Move Threshold

Automatic Combat Roster voice routing now begins only when **7 or more eligible players** are mustered.

- Fewer than 6: no Combat Roster is generated (existing behavior).
- Exactly 6: the Combat Roster may be posted, but all six remain together in the Ready Room; no automatic voice move occurs.
- 7 or more: normal V77 behavior applies — side detection, roster post, 60-second staging delay, then automatic voice routing.
- Automatic U.S./NVA detection and the U.S.-only helicopter rule are unchanged.
- Mid-game protection is unchanged.
- Manual `/combat-generate` remains an explicit staff override and can still perform routing when intentionally invoked.
