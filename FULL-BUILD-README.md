# 5th CAV Battalion Clerk — Strict Company / Platoon / Squad Access

This build extends strict Discord assignment visibility through all three organizational levels.

## Access model
- A Company cannot see B/C Company internal areas.
- A Company • 1st Platoon cannot see A Company • 2nd/3rd/4th Platoon areas.
- A Company • 1st Platoon • 1st Squad can see only its own squad text/voice channels inside the platoon area.
- Platoon-wide channels remain visible to every Soldier assigned to that exact platoon.
- Command, S-1, S-3, and appropriate leadership retain oversight access.
- Rank, MOS, and qualification roles remain permission-neutral.

## Migration
After deployment run:
`/strict-access-rebuild confirm:True`

The command removes legacy generic platoon/squad assignment roles where possible, creates the company/platoon/squad-specific roles, and reapplies managed channel permissions.

Then run:
`/structure-status`

Optionally run:
`/permissions-repair confirm:True`
