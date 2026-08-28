# Command Desk Guided Work Update — 2026-08-27

This cumulative website build extends the Command Desk ease-of-use overhaul.

## Added
- DO THIS NEXT command priority block sourced from the existing live attention queue.
- Red/overdue work is prioritized ahead of lower-priority staff work.
- Workflow queues for:
  - Waiting on Command
  - New Discord Arrivals
  - Waiting on Assignment
  - Ready for Promotion
  - M16 Action Due
  - Discord Sync Problems
  - Overdue Staff Work
- One-glance system health for Website/DB, Battalion Clerk heartbeat, Discord personnel sync, HLL telemetry, and Recruiting intake.
- Health and queue checks are fail-soft so a secondary check cannot take down Command Desk.
- Global Soldier-search prompt now explicitly supports name, BR number, Discord, Steam, and HLL identity.
- Responsive/mobile layouts for the new priority and workflow controls.

## Authority / integrity
- No new personnel source of truth was introduced.
- Existing Recruiting, Accessions, Replacement Detachment, Promotion Board, S-4, Discord sync, HLL telemetry, and personnel-action systems remain authoritative.
- Existing permissions are retained; health links route non-owning staff to the reliability board instead of unauthorized section pages.
