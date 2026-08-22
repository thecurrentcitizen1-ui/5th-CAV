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

RECRUITING PIPELINE
- /verify-application code:<CAV-XXXX>
- /application-status
- New walk-ins receive Prospective Replacement when the role exists.
- Approved cases receive Approved Replacement and are still not personnel until rank+MOS+assignment roles settle.
- Run /structure-repair after deploy to add the new Recruiting Status roles to an existing server.

FINAL RECRUITING FLOW — DISCORD OAUTH
1. Applicant opens/fills the website application and verifies Discord identity with OAuth identify-only.
2. Applicant submits the application and receives the normal Discord invite option.
3. On joining Discord, Battalion Clerk matches the permanent Discord user ID and assigns Prospective Replacement.
4. Battalion Headquarters reviews the Recruiting Case.
5. Approval automatically swaps Prospective Replacement to Approved Replacement and DMs the applicant.
6. Staff assigns rank, MOS, company, platoon, and squad roles.
7. Every role edit restarts the 30-second settle timer.
8. After a valid approved role set settles, Battalion Clerk creates/links the Soldier record, delivers credentials, files conversion, and removes recruiting holding roles.
9. Denied/closed/enlisted cases cannot regain recruiting holding roles from later role changes.
