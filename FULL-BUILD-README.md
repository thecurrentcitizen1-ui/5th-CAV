# Battalion Clerk — Direct Entry Rank / Strict Assignment Intake

- Keeps the 30-second Discord role settle window.
- Reads the full settled rank, MOS, company, platoon, and squad role set before personnel creation.
- Fixes strict company/platoon/squad validation so holding the correct parent + child roles is not falsely treated as duplicate assignments.
- Entry rank is treated as the Soldier's actual starting grade. The website personnel record remains authoritative after creation.
- Existing 201 Files are never silently re-ranked from Discord role drift.
- New-member DM explicitly states that the promotion track starts from the entry grade and lower-rank history is not invented.
