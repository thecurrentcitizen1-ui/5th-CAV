# V95 — Slash Command Limit / Server Watch Sync Fix
Date: 2026-09-09
Baseline: Battalion Clerk V94

## Confirmed cause
V94 contained 105 top-level Discord chat-input commands. Discord supports a maximum of 100 top-level chat-input application commands per scope, causing the command-tree synchronization to fail and preventing newly added Server Watch commands from appearing.

## Fix
Reduced the top-level command count to 96 without removing functional systems by grouping related controls:
- `/server-watch status`
- `/server-watch test`
- `/seeding set-channel`
- `/seeding status`
- `/seeding clear-channel`
- `/server-message send`
- `/server-message clear`
- `/battalionbrief setup|status|preview|post|off`

The deprecated `/hll-unlink` alias was retired; `/unlink-game` remains the supported member command.

## Validation
- Python compile: PASS
- Estimated top-level application command count: 96
- Server Watch group contains status + test: PASS
- Existing V94 application reminders and Discord removal queue retained.
